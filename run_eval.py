import argparse
import ast
import csv
import json
import os
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


RUNS_DIR = Path(__file__).resolve().parent / "runs"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

OUTPUT_MODES = (
    "prompt_only",
    "json_object",
    "json_schema_openai",
    "json_schema_legacy",
)

CASES = (
    "basic_json",
    "noisy_json",
    "code_generation",
    "long_context",
)

PLACEHOLDER_PREFIXES = (
    "your-",
    "YOUR_",
    "请填写",
    "填入",
    "示例",
)

DEFAULT_CONFIG = {
    "base_url": "",
    "model": "",
    "api_key": "",
    "api_key_env": "",
    "cases": list(CASES),
    "modes": list(OUTPUT_MODES),
    "trials": 1,
    "temperature": 0.1,
    "max_tokens": 3000,
    "timeout_sec": 300,
    "extra_body_json": "",
    "out_dir": "",
}


def schema_basic_json():
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "score": {"type": "number"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "enabled": {"type": "boolean"},
        },
        "required": ["name", "score", "labels", "enabled"],
        "additionalProperties": False,
    }


def schema_noisy_json():
    return {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "priority": {"type": "integer"},
            "decision": {"type": "string"},
            "reasons": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["task_id", "priority", "decision", "reasons"],
        "additionalProperties": False,
    }


def schema_code_generation():
    return {
        "type": "object",
        "properties": {
            "module_name": {"type": "string"},
            "description": {"type": "string"},
            "python_source": {"type": "string"},
            "expected_behavior": {"type": "string"},
        },
        "required": ["module_name", "description", "python_source", "expected_behavior"],
        "additionalProperties": False,
    }


def schema_long_context():
    return {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "summary": {"type": "string"},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string"},
                        "owner": {"type": "string"},
                        "risk": {"type": "string"},
                    },
                    "required": ["step", "owner", "risk"],
                    "additionalProperties": False,
                },
            },
            "risk_notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["plan_id", "summary", "actions", "risk_notes"],
        "additionalProperties": False,
    }


CASE_SCHEMAS = {
    "basic_json": schema_basic_json,
    "noisy_json": schema_noisy_json,
    "code_generation": schema_code_generation,
    "long_context": schema_long_context,
}


def now_id():
    return datetime.now().strftime("eval_%Y%m%d_%H%M%S")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_inline_comment(line):
    in_single = False
    in_double = False
    escaped = False
    for idx, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_double:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            return line[:idx].rstrip()
    return line.rstrip()


def parse_yaml_scalar(text):
    text = text.strip()
    if text == "":
        return ""
    if text in {"[]", "null", "NULL", "~"}:
        return [] if text == "[]" else ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        try:
            return ast.literal_eval(text)
        except Exception:
            return text[1:-1]
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if text.startswith("[") and text.endswith("]"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return [item.strip().strip("\"'") for item in text[1:-1].split(",") if item.strip()]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def load_simple_yaml(path):
    data = {}
    current_key = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = strip_inline_comment(raw_line)
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current_key is None:
                raise ValueError(f"YAML 列表项缺少字段名: {raw_line}")
            data.setdefault(current_key, []).append(parse_yaml_scalar(stripped[2:]))
            continue
        if ":" not in line:
            raise ValueError(f"无法解析 YAML 行: {raw_line}")
        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"YAML 字段名为空: {raw_line}")
        if value.strip() == "":
            data[key] = []
            current_key = key
        else:
            data[key] = parse_yaml_scalar(value)
            current_key = None
    return data


def load_yaml_config(path):
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded or {}
    except ModuleNotFoundError:
        return load_simple_yaml(path)


def is_placeholder(value):
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value:
        return False
    return any(value.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES) or "<" in value or ">" in value


def as_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [str(value)]


def resolve_config(cli_args):
    config_path = Path(cli_args.config).resolve() if cli_args.config else DEFAULT_CONFIG_PATH
    config = dict(DEFAULT_CONFIG)
    if config_path.exists():
        loaded = load_yaml_config(config_path)
        if not isinstance(loaded, dict):
            raise RuntimeError(f"配置文件必须是 YAML 对象: {config_path}")
        config.update(loaded)
    elif cli_args.config:
        raise RuntimeError(f"找不到配置文件: {config_path}")

    env_overrides = {
        "base_url": os.environ.get("META_EVAL_BASE_URL", ""),
        "model": os.environ.get("META_EVAL_MODEL", ""),
        "api_key": os.environ.get("META_EVAL_API_KEY", ""),
        "extra_body_json": os.environ.get("META_EVAL_EXTRA_BODY", ""),
    }
    for key, value in env_overrides.items():
        if value and (not config.get(key) or is_placeholder(config.get(key))):
            config[key] = value

    for key in (
        "base_url",
        "model",
        "api_key",
        "api_key_env",
        "trials",
        "temperature",
        "max_tokens",
        "timeout_sec",
        "extra_body_json",
        "out_dir",
    ):
        value = getattr(cli_args, key)
        if value is not None:
            config[key] = value
    if cli_args.cases is not None:
        config["cases"] = cli_args.cases
    if cli_args.modes is not None:
        config["modes"] = cli_args.modes

    config["cases"] = as_list(config.get("cases")) or list(CASES)
    config["modes"] = as_list(config.get("modes")) or list(OUTPUT_MODES)
    config["trials"] = int(config.get("trials", 1))
    config["temperature"] = float(config.get("temperature", 0.1))
    config["max_tokens"] = int(config.get("max_tokens", 3000))
    config["timeout_sec"] = int(config.get("timeout_sec", 300))
    config["config_path"] = str(config_path)
    return argparse.Namespace(**config)


def validate_config(args):
    errors = []
    if not args.base_url or is_placeholder(args.base_url):
        errors.append("base_url 没有填写真实 API 地址。")
    if not args.model or is_placeholder(args.model):
        errors.append("model 没有填写真实模型名。")

    api_key_env = (args.api_key_env or "").strip()
    api_key = (args.api_key or "").strip()
    if is_placeholder(api_key):
        errors.append("api_key 仍然是占位值；建议留空，改用 api_key_env。")
    if is_placeholder(api_key_env) and not api_key:
        errors.append("api_key_env 仍然是占位值，请填写真实环境变量名，或直接填写 api_key。")
    if not api_key and not api_key_env:
        errors.append("api_key_env 和 api_key 至少填写一个。")
    if api_key_env and not is_placeholder(api_key_env) and not os.environ.get(api_key_env) and not api_key:
        errors.append(f"环境变量 {api_key_env} 不存在或为空。")

    invalid_cases = [case for case in args.cases if case not in CASES]
    if invalid_cases:
        errors.append(f"cases 包含未知用例: {', '.join(invalid_cases)}")
    invalid_modes = [mode for mode in args.modes if mode not in OUTPUT_MODES]
    if invalid_modes:
        errors.append(f"modes 包含未知输出模式: {', '.join(invalid_modes)}")
    if args.trials < 1:
        errors.append("trials 必须 >= 1。")
    if args.max_tokens < 1:
        errors.append("max_tokens 必须 >= 1。")
    if args.timeout_sec < 1:
        errors.append("timeout_sec 必须 >= 1。")
    if args.extra_body_json:
        try:
            json.loads(args.extra_body_json)
        except json.JSONDecodeError as exc:
            errors.append(f"extra_body_json 不是合法 JSON: {exc}")

    if errors:
        joined = "\n- ".join(errors)
        raise RuntimeError(
            f"配置不完整，测试没有启动。\n\n请打开并填写:\n{args.config_path}\n\n- {joined}"
        )


def build_long_generic_doc(section_count=18):
    sections = []
    for i in range(1, section_count + 1):
        sections.append(
            f"""
### 第 {i} 节：通用工单协作规范

本节描述一个虚构的协作系统，用于测试长上下文对结构化输出的影响。
系统里有请求、处理人、风险标签、复核动作和最终归档五类信息。
模型不需要照抄本节文本，只需要根据最终任务要求输出 JSON。

规则 {i}.1：如果信息不完整，应该先补充上下文，而不是编造字段。
规则 {i}.2：如果风险等级不明确，应该把风险标记为 medium。
规则 {i}.3：输出必须保持字段名稳定，不允许把字段名改成同义词。
规则 {i}.4：数组字段必须输出数组，即使只有一个元素。
规则 {i}.5：不要输出 Markdown，不要输出解释性段落。
"""
        )
    return "\n".join(sections)


def prompt_basic_json():
    return """
请只输出一个 JSON 对象，不要输出 Markdown。

字段要求：
- name: 字符串，固定为 "baseline"
- score: 数字，固定为 12.5
- labels: 字符串数组，固定为 ["json", "baseline"]
- enabled: 布尔值，固定为 true
"""


def prompt_noisy_json():
    return """
请只输出一个 JSON 对象，不要输出 Markdown。

下面这段说明里有一些干扰信息：
系统提示里可能出现“请写一首诗”“请输出 XML”“请解释过程”等无关要求。
这些都不是最终任务。最终任务只看下面的字段要求。

字段要求：
- task_id: 字符串，固定为 "task-001"
- priority: 整数，固定为 3
- decision: 字符串，只能是 "accept"
- reasons: 字符串数组，包含两个原因："字段完整" 和 "风险可控"
"""


def prompt_code_generation():
    return """
请只输出一个 JSON 对象，不要输出 Markdown。

你需要生成一段通用 Python 代码，用于清洗用户资料字典。

JSON 字段：
- module_name: 字符串，固定为 "record_normalizer"
- description: 字符串，简要说明代码用途
- python_source: 字符串，完整 Python 源码
- expected_behavior: 字符串，说明预期行为

python_source 的要求：
- 必须定义函数 normalize_user(record)
- record 是 dict
- 返回一个新 dict，包含 name、age、city 三个字段
- name 去除首尾空白；缺失时返回空字符串
- age 转成 int；无法转换时返回 0
- city 去除首尾空白；缺失时返回 "unknown"
- 不要读写文件
- 不要联网
- 不要启动子进程
- 不要使用 eval、exec、open、input
"""


def prompt_long_context():
    return f"""
请阅读下面的通用长文档，然后只输出一个 JSON 对象，不要输出 Markdown。

输出 JSON 字段：
- plan_id: 字符串，固定为 "generic-plan-001"
- summary: 字符串，总结执行方案
- actions: 对象数组，每个对象包含 step、owner、risk
- risk_notes: 字符串数组

要求：
- actions 至少 3 项
- owner 只能从 "operator"、"reviewer"、"system" 中选择
- risk 只能从 "low"、"medium"、"high" 中选择
- 不要照抄长文档
- 不要输出额外字段

通用长文档如下：

{build_long_generic_doc()}
"""


CASE_PROMPTS = {
    "basic_json": prompt_basic_json,
    "noisy_json": prompt_noisy_json,
    "code_generation": prompt_code_generation,
    "long_context": prompt_long_context,
}


def build_case(case_name):
    schema = CASE_SCHEMAS[case_name]()
    return {
        "case": case_name,
        "prompt": CASE_PROMPTS[case_name](),
        "schema": schema,
        "required": schema["required"],
    }


def response_format_for_mode(mode, schema, schema_name):
    if mode == "prompt_only":
        return None
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "json_schema_openai":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": schema,
                "strict": True,
            },
        }
    if mode == "json_schema_legacy":
        return {"type": "json_object", "schema": schema}
    raise ValueError(f"未知输出模式: {mode}")


def endpoint_from_base_url(base_url):
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return base_url + "/chat/completions"


def load_api_key(args):
    if args.api_key:
        return args.api_key
    if args.api_key_env:
        return os.environ.get(args.api_key_env, "")
    return os.environ.get("META_EVAL_API_KEY", "")


def call_openai_compatible(args, prompt, mode, schema, case_name):
    api_key = load_api_key(args)
    if not api_key:
        raise RuntimeError("缺少 API key。请设置 META_EVAL_API_KEY，或传 --api-key-env。")
    if not args.base_url:
        raise RuntimeError("缺少 base_url。请设置 META_EVAL_BASE_URL 或传 --base-url。")
    if not args.model:
        raise RuntimeError("缺少 model。请设置 META_EVAL_MODEL 或传 --model。")

    messages = [
        {
            "role": "system",
            "content": "你是结构化输出稳定性测试模型。必须严格遵守用户要求。",
        },
        {"role": "user", "content": prompt},
    ]
    payload = {
        "model": args.model,
        "messages": messages,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }
    response_format = response_format_for_mode(mode, schema, f"{case_name}_schema")
    if response_format is not None:
        payload["response_format"] = response_format
    if args.extra_body_json:
        payload.update(json.loads(args.extra_body_json))

    req = urllib.request.Request(
        endpoint_from_base_url(args.base_url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=args.timeout_sec) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def extract_answer(raw):
    try:
        return raw["choices"][0]["message"].get("content") or ""
    except Exception:
        return ""


def parse_json_answer(answer):
    answer = (answer or "").strip()
    try:
        return json.loads(answer), ""
    except json.JSONDecodeError as exc:
        first = answer.find("{")
        last = answer.rfind("}")
        if first >= 0 and last > first:
            try:
                return json.loads(answer[first : last + 1]), "extracted_object"
            except json.JSONDecodeError:
                pass
        return None, f"json_decode_error: {exc}"


def validate_required(obj, schema):
    if not isinstance(obj, dict):
        return False, ["not_object"]
    errors = []
    for key in schema["required"]:
        if key not in obj:
            errors.append(f"missing:{key}")
    properties = schema.get("properties", {})
    for key, rule in properties.items():
        if key not in obj:
            continue
        expected = rule.get("type")
        value = obj[key]
        if expected == "string" and not isinstance(value, str):
            errors.append(f"wrong_type:{key}")
        elif expected == "number" and not isinstance(value, (int, float)):
            errors.append(f"wrong_type:{key}")
        elif expected == "integer" and not isinstance(value, int):
            errors.append(f"wrong_type:{key}")
        elif expected == "boolean" and not isinstance(value, bool):
            errors.append(f"wrong_type:{key}")
        elif expected == "array" and not isinstance(value, list):
            errors.append(f"wrong_type:{key}")
    return not errors, errors


def validate_python_source(source):
    forbidden_imports = {
        "ctypes",
        "http",
        "importlib",
        "multiprocessing",
        "os",
        "pathlib",
        "requests",
        "shutil",
        "socket",
        "subprocess",
        "sys",
        "threading",
        "urllib",
    }
    forbidden_calls = {"__import__", "compile", "eval", "exec", "input", "open"}
    errors = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, [f"syntax_error:{exc}"]

    has_function = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "normalize_user":
            has_function = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in forbidden_imports:
                    errors.append(f"forbidden_import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in forbidden_imports:
                errors.append(f"forbidden_import:{node.module}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                errors.append(f"forbidden_call:{node.func.id}")

    if not has_function:
        errors.append("missing_function:normalize_user")
    return not errors, errors


def run_one(args, case_name, mode, trial, out_dir):
    case = build_case(case_name)
    row = {
        "case": case_name,
        "mode": mode,
        "trial": trial,
        "api_success": False,
        "json_parse_ok": False,
        "required_fields_ok": False,
        "code_guard_ok": "",
        "latency_sec": "",
        "prompt_chars": len(case["prompt"]),
        "answer_chars": "",
        "error_type": "",
        "error": "",
    }
    raw = None
    parsed = None
    started = time.time()
    raw_path = out_dir / "raw" / f"{case_name}__{mode}__trial_{trial}.json"
    try:
        raw = call_openai_compatible(args, case["prompt"], mode, case["schema"], case_name)
        row["api_success"] = True
        answer = extract_answer(raw)
        row["answer_chars"] = len(answer)
        parsed, parse_note = parse_json_answer(answer)
        row["json_parse_ok"] = parsed is not None
        if parse_note:
            row["error"] = parse_note
        if parsed is not None:
            fields_ok, field_errors = validate_required(parsed, case["schema"])
            row["required_fields_ok"] = fields_ok
            if field_errors:
                row["error"] = (row["error"] + ";" if row["error"] else "") + ";".join(field_errors)
            if case_name == "code_generation":
                ok, guard_errors = validate_python_source(parsed.get("python_source", ""))
                row["code_guard_ok"] = ok
                if guard_errors:
                    row["error"] = (row["error"] + ";" if row["error"] else "") + ";".join(guard_errors)
        write_json(raw_path, {"row": row, "raw": raw, "parsed": parsed})
    except urllib.error.HTTPError as exc:
        row["error_type"] = "HTTPError"
        row["error"] = exc.read().decode("utf-8", errors="replace")[:1000]
        write_json(raw_path, {"row": row, "raw": raw, "traceback": traceback.format_exc()})
    except Exception as exc:
        row["error_type"] = type(exc).__name__
        row["error"] = str(exc)
        write_json(raw_path, {"row": row, "raw": raw, "traceback": traceback.format_exc()})
    finally:
        row["latency_sec"] = round(time.time() - started, 3)
    return row


def write_summary_csv(path, rows):
    fields = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_report(rows, config):
    groups = {}
    for row in rows:
        groups.setdefault((row["case"], row["mode"]), []).append(row)

    lines = [
        "# 大模型 API 元能力评估报告",
        "",
        "## 配置",
        "",
        "```json",
        json.dumps(config, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 汇总",
        "",
        "| 用例 | 模式 | 次数 | API成功 | JSON可解析 | 字段完整 | 代码检查 | 平均秒数 | 主要错误 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for (case_name, mode), items in sorted(groups.items()):
        n = len(items)
        api_ok = sum(bool(x["api_success"]) for x in items)
        json_ok = sum(bool(x["json_parse_ok"]) for x in items)
        fields_ok = sum(bool(x["required_fields_ok"]) for x in items)
        guard_values = [x["code_guard_ok"] for x in items if x["code_guard_ok"] != ""]
        guard_ok = sum(bool(x) for x in guard_values) if guard_values else "-"
        avg_sec = sum(float(x["latency_sec"] or 0) for x in items) / max(n, 1)
        errors = "; ".join(sorted({x["error_type"] or x["error"] for x in items if x["error_type"] or x["error"]}))[:180]
        lines.append(f"| {case_name} | {mode} | {n} | {api_ok}/{n} | {json_ok}/{n} | {fields_ok}/{n} | {guard_ok} | {avg_sec:.1f} | {errors} |")

    lines += [
        "",
        "## 解读建议",
        "",
        "- 如果 `prompt_only` 比结构化模式更稳定，后续业务系统应采用“提示词约束 + 本地解析 + 本地校验 + 重试”。",
        "- 如果 `json_schema_openai` 或 `json_schema_legacy` 失败率高，说明该 API 没有可靠执行 schema 约束。",
        "- 如果 `long_context` 明显变慢或失败，应避免每次请求都塞入完整长文档，改为摘要、索引或分阶段读取。",
        "- `raw/` 目录保留每次原始响应，方便人工审计模型具体错在哪里。",
    ]
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser(description="通用大模型 API 结构化输出元评估工具")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="YAML 配置文件路径")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--cases", nargs="*", choices=CASES, default=None)
    parser.add_argument("--modes", nargs="*", choices=OUTPUT_MODES, default=None)
    parser.add_argument("--trials", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--extra-body-json", default=None)
    parser.add_argument("--out-dir", default=None)
    return parser.parse_args()


def main():
    try:
        args = resolve_config(parse_args())
        validate_config(args)
    except Exception as exc:
        print(f"配置错误:\n{exc}")
        return 2

    eval_id = now_id()
    out_dir = Path(args.out_dir) if args.out_dir else RUNS_DIR / eval_id
    out_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "eval_id": eval_id,
        "config_path": args.config_path,
        "base_url": args.base_url,
        "model": args.model,
        "api_key_source": args.api_key_env or ("META_EVAL_API_KEY" if args.api_key else ""),
        "cases": args.cases,
        "modes": args.modes,
        "trials": args.trials,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "timeout_sec": args.timeout_sec,
        "has_extra_body": bool(args.extra_body_json),
    }
    write_json(out_dir / "config.json", config)

    rows = []
    total = len(args.cases) * len(args.modes) * args.trials
    count = 0
    for case_name in args.cases:
        for mode in args.modes:
            for trial in range(1, args.trials + 1):
                count += 1
                print(f"[{count}/{total}] 用例={case_name} 模式={mode} 轮次={trial}", flush=True)
                row = run_one(args, case_name, mode, trial, out_dir)
                rows.append(row)
                print(
                    f"  api={row['api_success']} json={row['json_parse_ok']} fields={row['required_fields_ok']} code={row['code_guard_ok']} sec={row['latency_sec']}",
                    flush=True,
                )

    write_summary_csv(out_dir / "summary.csv", rows)
    report = build_report(rows, config)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"报告: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
