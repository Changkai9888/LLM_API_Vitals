《这是一个：通用大模型 API 结构化输出元评估工具》
你还在因为，换一个模型就不知道 JSON 还稳不稳、Schema 还听不听话、长上下文会不会把输出带跑偏，而感到苦恼吗？先别凭感觉猜，给接口做一次元能力体检吧！

这是一个可独立迁移的通用大模型 API 稳定性评估包，先填写 `model_api_eval/config.yaml`，再运行 `python -m model_api_eval.run_eval`，它会按默认 4 个测试用例×4种输出模式 = 16 次 API 调用目标接口，并在 `model_api_eval/runs/` 生成 JSON 结果、分析报告文件report.md 和 模式对比表summary.csv；默认用例覆盖最小 JSON、干扰文本抽取、代码生成、长上下文四个截面，用来判断 prompt-only、json_object、json_schema 等调用方式哪种更稳。

我（们）做的这是一个，中文、零依赖、傻瓜式、专测 OpenAI 兼容 API 的结构化输出稳定性小体检包（不是很严肃的那一种，至少现在来看是这样的）。

# 通用大模型 API 元能力评估包：
中文名：模型体检台
英文名：LLM_API_Vitals

这个目录是一个独立、可迁移、可开源的测试包，用来评估不同大模型 API 在“结构化输出”和“长上下文干扰”场景下的稳定性。

它不依赖当前项目的任何业务代码、业务文档或私有协议。

## 评估目标

这个工具重点评估：

```text
1. 普通提示词要求 JSON 是否稳定
2. response_format=json_object 是否稳定
3. response_format=json_schema 是否稳定
4. 长文本上下文是否干扰 JSON 结构
5. 生成代码是否能通过基础静态检查
6. 每种调用方式的耗时和失败类型
```

## 内置测试用例

```text
basic_json
  最小 JSON 对象测试，用来检测基本字段稳定性。

noisy_json
  带干扰文本的 JSON 提取测试，用来检测模型是否会被无关说明影响。

code_generation
  通用 Python 函数生成测试，用来检测 JSON 包裹代码时是否稳定。

long_context
  通用长文档上下文测试，用来检测长文本对结构化输出的影响。
```

所有测试内容都是通用虚构任务，不包含任何特定行业、特定业务或私有系统细节。

## 输出模式

```text
prompt_only
  不传 response_format，只在提示词里要求“只输出一个 JSON 对象”。

json_object
  传 response_format={"type": "json_object"}。

json_schema_openai
  使用 OpenAI 标准 json_schema 格式：
  response_format={"type":"json_schema","json_schema":...}

json_schema_legacy
  使用一些兼容 API 常见的旧式 schema 格式：
  response_format={"type":"json_object","schema":...}
```

## 配置 API

默认读取同目录的 `config.yaml`。第一次运行前，先打开这个文件，填写下面三项：

```yaml
base_url: "你的 OpenAI 兼容 API 地址"
model: "你的模型名"
api_key_env: "保存 API key 的环境变量名"
```

不建议把真实密钥直接写进配置文件；推荐先在系统环境变量里保存密钥，然后只把环境变量名写进 `api_key_env`。

Windows PowerShell 示例：

```powershell
$env:YOUR_API_KEY_ENV_NAME="你的真实密钥"
```

如果 `config.yaml` 里的关键字段仍是占位值，程序会直接报错并说明缺哪个字段，不会开始调用 API。

仍然可以用命令行临时覆盖配置：

```powershell
python -m model_api_eval.run_eval --trials 3 --modes prompt_only json_schema_openai
```

如果某个兼容接口需要额外请求字段，在 `config.yaml` 里填写一行 JSON：

```yaml
extra_body_json: "{\"reasoning_effort\":\"high\"}"
```

## 快速运行

填好 `config.yaml` 后运行：

```powershell
python -m model_api_eval.run_eval
```

更有参考价值的小样本：

```powershell
python -m model_api_eval.run_eval --trials 3
```

只测部分用例：

```powershell
python -m model_api_eval.run_eval --cases basic_json code_generation --trials 2
```

只测部分输出模式：

```powershell
python -m model_api_eval.run_eval --modes prompt_only json_schema_openai --trials 2
```

## 输出结果

默认写入：

```text
model_api_eval/runs/<eval_id>/
  config.json
  summary.csv
  report.md
  raw/
```

其中：

```text
summary.csv
  机器可读的逐次测试结果。

report.md
  中文汇总报告。

raw/
  每次 API 调用的原始响应和解析结果，方便审计。
```

## 推荐解读方式

优先看：

```text
json_parse_ok
required_fields_ok
code_guard_ok
latency_sec
error_type
error
```

如果 `json_schema_openai` 或 `json_object` 的成功率低于 `prompt_only`，说明该 API 的结构化输出能力不可靠，后续工作流应使用：

```text
严格提示词 + 本地 JSON 解析 + 本地字段校验 + 本地重试
```

如果 `long_context` 明显失败率更高或耗时更长，说明后续业务系统不应该把完整长文档直接塞进每次生成提示词，而应先抽取协议摘要。

## 开源协议

本项目采用 **Apache License 2.0** 开源协议，使用、修改、分发本项目时必须保留版权声明和许可证文本。

```text
Copyright (c) 2026 <李昌开实验室>

Licensed under the Apache License, Version 2.0.
You may obtain a copy of the License at:

https://www.apache.org/licenses/LICENSE-2.0
```
