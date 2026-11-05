# 公共模型调用

本包负责模型配置与 HTTP 调用，不依赖 `agents/`，不包含需求字段、回复格式或飞书业务逻辑。

- `DeepSeekConfig`：读取 `DEEPSEEK_*` 环境变量，验证配置，API Key 不出现在对象的默认文本表示中。
- `JsonCompletionClient`：结构化输出调用接口 `complete(system_prompt, user_text) -> str`。
- `DeepSeekClient`：实现 DeepSeek 的 JSON 输出调用；使用 HTTPS、可配置超时和响应大小上限，不跟随重定向，不隐式重试。
- `ModelError`：统一异常与 `retryable` 标记，错误内容不包含 Key、服务商响应正文或用户原文。

各 Agent 自行提供提示词、验证返回的业务结构、保存业务进度并决定如何重试。Speaker 的这些行为在 `agents/speaker_agent/intake/` 和 `orchestration/` 中实现，回复和需求单仍由其 `presentation/` 和 `templates/` 管理。

```python
from shared.llm.config import DeepSeekConfig
from shared.llm.deepseek import DeepSeekClient

client = DeepSeekClient(DeepSeekConfig.from_env())
# 由调用方先加载 .env；complete 会发起实际 API 请求。
content = client.complete("按约定的结构输出 JSON。", "待整理文本")
```

接口依据：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。测试通过替身拦截网络，不使用真实 API Key。
