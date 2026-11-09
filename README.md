# logtriage

把日志变成一份能直接干活的故障报告：聚类报错、定位到项目代码的具体行、命中错误知识库、给出修复建议，并把结论发到飞书 / 钉钉 / 企业微信 / Telegram / 邮箱。

零第三方依赖，只需要 Python 3.11+。没有 LLM API key 也能完整运行，只是少了模型对根因的深入分析。

## 它能做什么

- **多格式日志解析**：普通文本日志、JSON 行日志、Python / Java / Node / C# / Go 堆栈、access log、多行异常。
- **错误聚类**：把同一类报错归并成一组，忽略 request id、时间戳、UUID、IP 等会变化的字段，统计次数和首末时间。
- **项目代码定位**：从堆栈和日志消息里提取文件/行号，解析到项目源码，输出报错行上下文；还会补上函数定义、符号引用。
- **错误知识库**：内置 75 条常见错误规则（Python、Java、Node、Go、.NET、数据库、HTTP、K8s、系统、Kafka 等），按异常类型、消息、关键字、日志级别、HTTP 状态码匹配，给出可能原因和修复建议。
- **可选 LLM 分析**：把最严重的几组错误连同堆栈、代码片段交给 OpenAI 兼容接口（默认 DeepSeek），生成根因、代码分析和修复建议。
- **通知**：控制台、通用 webhook、飞书、钉钉、企业微信、Telegram、SMTP 邮件；支持严重度阈值和冷却去重，避免同一条错误反复轰炸。
- **三种输出**：Markdown 报告、纯文本摘要、JSON（方便接其他系统）。
- **持续观察**：`watch` 轮询日志，只报告新增或次数增长的错误；`check` 适合 cron / CI，用退出码表达结果。

## 快速开始

```powershell
git clone https://github.com/yuechouu/logtriage.git
cd logtriage

# 不接 LLM，先看效果
python -m logtriage analyze examples/demo.log --project examples/demo_project --no-llm

# 输出纯文本摘要
python -m logtriage analyze examples/demo.log --project examples/demo_project --no-llm -f text

# 生成 Markdown 报告文件
python -m logtriage analyze examples/demo.log --project examples/demo_project --no-llm -o examples/demo-report.md
```

示例日志里有 8 类真实风格的错误，报告会定位到 `app/orders.py:23`、`app/payments.py:8`、`app/server.js:6`、`OrderService.java:15`、`worker/tasks.py:24` 等行。

安装成命令（可选）：

```powershell
python -m pip install -e .
logtriage analyze app.log --project .
```

也可以不开项目索引，只按日志内容推断：

```powershell
logtriage analyze app.log --no-project
```

## 常用命令

```text
logtriage analyze <日志...>   分析并输出报告
logtriage check <日志...>     只输出一行结论，用退出码表示是否达阈值
logtriage watch <日志...>     持续盯着日志，出现新错误时提醒
logtriage init                生成 logtriage.toml 配置模板
logtriage rules               查看内置错误规则（--json 可机器读取）
```

`analyze` 常用参数：

| 参数 | 说明 |
|---|---|
| `--project DIR` | 项目代码根目录 |
| `--no-project` | 不索引代码，只分析日志 |
| `--no-llm` | 禁用 LLM，只用规则和代码索引 |
| `-f markdown\|text\|json` | 输出格式，默认 markdown |
| `-o FILE` | 写入文件 |
| `--notify` | 分析后发送通知 |
| `--include-warnings` | 把 WARN 也纳入分析 |
| `--fail-on low\|medium\|high\|critical\|none` | 退出码阈值，默认 high |
| `-q` | 不打印报告，适合只发通知 |

退出码：`0` 正常（或未达阈值），`1` 达到 `--fail-on` 阈值，`2` 用法或配置错误，`130` 被 Ctrl+C 中断。

## 配置

```powershell
python -m logtriage init      # 在当前目录生成 logtriage.toml
```

程序按这个顺序找配置：`-c/--config` 指定路径 → 环境变量 `LOGTRIAGE_CONFIG` → 当前目录 `logtriage.toml` → `.logtriage.toml`。命令行参数会覆盖配置里的项目路径和 LLM 开关。

### LLM

```toml
[llm]
enabled = true
base_url = "https://api.deepseek.com"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"
max_groups = 5        # 最多让模型深挖几组，控制成本
timeout = 45
```

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
logtriage analyze app.log --project .
```

也可以直接写 `api_key`，但不建议提交到仓库。任何 OpenAI 兼容接口都可以，改 `base_url` 和 `model` 即可。

> 启用 LLM 时，最严重的若干组错误的堆栈、消息片段和相关代码片段会发送到所配置的接口。敏感项目请用 `--no-llm`，或换成内网模型。

### 通知

```toml
[notify]
enabled = true
min_severity = "high"      # 只有 high/critical 才发
cooldown_minutes = 30      # 同一指纹 30 分钟内不重复发

[[notify.channels]]
kind = "console"
enabled = true

# 飞书 / 钉钉 / 企业微信 / 通用 webhook
[[notify.channels]]
kind = "webhook"
url = "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
style = "feishu"           # generic | feishu | dingtalk | wecom
secret = ""                # 开启加签时填写

# Telegram
[[notify.channels]]
kind = "telegram"
token_env = "TELEGRAM_BOT_TOKEN"
chat_id = "123456789"

# 邮件
[[notify.channels]]
kind = "email"
host = "smtp.example.com"
port = 465
ssl = true
username = "alert@example.com"
password_env = "SMTP_PASSWORD"
from = "alert@example.com"
to = ["oncall@example.com"]
```

冷却状态默认写在当前目录的 `.logtriage_state.json`，记录每个错误指纹上次通知的时间和次数。删除它就会重新开始计时。

## 自定义错误规则

内置规则在 `logtriage/data/error_rules.json`。自定义规则可以放一个 JSON 文件，或一个目录（读取其中所有 `*.json`），然后在配置里引用：

```toml
[general]
extra_rules = ["E:/work/my-rules.json"]
```

文件格式：

```json
{
  "version": 1,
  "rules": [
    {
      "id": "payment-timeout",
      "title": "支付网关超时",
      "severity": "high",
      "tags": ["payment", "timeout"],
      "match": {
        "exception": "TimeoutError",
        "message": "payment gateway",
        "raw": "timed out",
        "level": "ERROR",
        "http_status": [502, 504]
      },
      "cause": "支付网关在超时时间内没有返回。",
      "fixes": [
        "检查支付网关健康状况和网络连通性。",
        "给重试设置上限和退避，避免雪崩。"
      ]
    }
  ]
}
```

`match` 里的字段是 AND 关系；`http_status` 可以写单个整数或数组。命中后会提升严重度、补充原因和修复建议，并把置信度抬高。规则 `id` 重复时先加载的优先。

## 支持哪些日志

- 带时间戳和级别的普通行：`2026-11-09 10:01:12 ERROR [order-api] app.orders - failed ...`
- Python / Java / Node / C# / Go 的 traceback 和 `Caused by` 链
- `Exception in thread "main" ...`
- access log：`10.0.0.1 - - [09/Nov/2026:10:07:01 +0800] "GET /x HTTP/1.1" 500 ...`
- JSON 行日志（含多行 `stack` / `traceback` 字段）
- 没有堆栈的裸异常行，会粘回上一条日志
- 目录会递归读取常见日志后缀；`-` 表示从标准输入读

解析不追求覆盖所有自定义格式；遇到不认识的行，至少会保留原文，不会丢日志。

## 项目结构

```text
logtriage/
  logtriage/
    cli.py         命令行入口
    analyze.py     分析编排：解析 → 聚类 → 规则 → 代码索引 → LLM
    parse.py       多格式日志解析
    detect.py      错误/警告识别与评分
    signature.py   消息归一化、指纹、错误分组
    project.py     项目代码索引、堆栈/符号/文本检索
    knowledge.py   错误规则库匹配
    llm.py         OpenAI 兼容客户端（仅标准库 urllib）
    render.py      Markdown / 文本 / JSON 渲染
    notify.py      webhook / Telegram / 邮件 / 冷却
    config.py      TOML 配置
    data/error_rules.json
  examples/        示例日志和示例项目
  tests/           unittest 测试
```

## 测试

```powershell
python -m unittest discover -s tests -t .
```

当前 54 个测试，覆盖解析、聚类、代码索引、规则匹配、LLM JSON 解析、渲染、通知冷却和 CLI。

## 限制

- 代码索引只做轻量静态扫描，不是编译器；动态语言里的调用链需要靠堆栈和文本匹配。
- 只根据日志推断时，没有代码上下文，结论会保守一些。
- `watch` 是轮询，不监听文件系统事件；默认不调用 LLM，避免持续烧钱。
- 通知发送失败会打印到 stderr，但不会让 `analyze` 的退出码失败（报告本身仍然有效）。

## License

MIT
