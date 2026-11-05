# 个人飞书私聊接入

已实现：别人直接私聊你的个人账号，Speaker 增量读取新消息，用 DeepSeek 识别需求、整理需求单、选择对应台账，交给下层分析和技术评审，再按模板回复原消息。无需添加机器人好友或配置机器人消息长连接。保留开放平台应用及 App ID / App Secret，用于用户 OAuth 授权和现有台账访问。

代码已通过本地替身测试；用户登录、租户权限与实际收发仍需在你的账号完成联调。

## 一次性配置

应用后台的机器人能力先保留/开启：[官方消息 API 总览](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1) 仍将其列为接口调用前置条件。用户身份接口的细则与机器人身份不同，当前不承诺禁用该能力也能完成全部收发。启用能力不等于用机器人发消息，个人模式始终显式使用用户 Token，不订阅机器人消息事件。

在现有应用「权限管理」中申请下列**用户身份**权限，并创建版本、发布/审批使其生效：

| 权限 | 用途 |
| --- | --- |
| `contact:user.base:readonly` | 获取授权账号基本信息 |
| `im:chat:read` | 自动枚举个人单聊会话 |
| `im:message` | 读取与发送消息的基础权限 |
| `im:message.p2p_msg:get_as_user` | 以用户身份读取单聊消息 |
| `im:message.send_as_user` | 以用户身份回复原会话 |

授权链接还会申请 `offline_access`，用于取得刷新令牌。可开通权限以本租户、应用后台为准；如果个人消息权限不可申请，需要管理员/飞书开放平台确认，程序不会转为机器人代发。

在「安全设置」的重定向 URL 中添加：

```text
http://127.0.0.1:8765/callback
```

`.env` 保留 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`DEEPSEEK_API_KEY`、`FEISHU_LEDGER_URLS`，并使用：

```dotenv
FEISHU_MESSAGE_MODE=personal
FEISHU_PERSONAL_POLL_SECONDS=10
FEISHU_ENABLE_CHAT_COMMANDS=true
FEISHU_ENABLE_POLLING=true
```

不需要手填会话 ID、用户 ID、`user_access_token` 或机器人 ID；不要把 App Secret、Token、Key 发送到聊天中。在项目根目录执行：

```powershell
python main.py --login
python main.py --check-user
python main.py --discover
python main.py
```

`--login` 打开浏览器，请用需要接入的个人账号同意授权，3 分钟内完成回调。使用运行程序的同一台电脑，端口 8765 不可占用。Windows 授权文件使用当前 Windows 账号加密保存于 `runtime/auth/feishu-user.dat`，正常到期自动刷新；刷新结果不确定或授权被撤销时重新登录。重新登录前先 Ctrl+C 停止项目。

`--check-user` 只读验证账号、会话枚举和最近一分钟消息读取，不输出消息内容，不发消息、不调用模型、不写台账；没有会话时无法验证历史读取权限，发送权限也必须在实际联调时验证。`--discover` 单独只读检查台账访问。`--check` 始终只检查本地配置。

## 台账仍需应用权限

当前台账、可选云文档继续使用应用身份，因此还需给应用配置 Wiki、电子表格/多维表格权限，以及目标文件的数据访问权限。

你的台账是 Wiki 中的普通电子表格，应开通节点读取 `wiki:node:read`，以及电子表格的读取、编辑权限，并允许应用访问该文件。若 `--discover` 报 `99991672`，按照输出缺少的权限在后台开通并发布版本。个人 OAuth 登录不会自动解决应用权限不足，无需把机器人加入聊天。

## 消息处理与测试

- 默认每 10 秒轮询。首次启动以本地保存的时间为起点，不把过去聊天全部建档；之后重启补读停机期间新消息。每个会话独立分页和游标，失败保留原位置；重叠 30 秒读取并按消息 ID 去重。
- 自己发送的消息、机器人消息、撤回消息会跳过。支持纯文本和富文本中的文字；图片、文件、语音及转发消息包内容尚未解析。
- 同轮收到的片段按会话合并，最后一条已读取消息至少过去 3 秒后再调用模型。普通闲聊、问候或感谢由模型识别后，统一发送 `templates/away.md` 中的固定回复：“本人不在，普通聊天 DeepSeek 暂时不回复。”；不创建需求。文案修改后重启生效，发送失败沿用持久化重试及原消息 UUID。
- 根据工作表名称或页签链接路由，也可配置 `SPEAKER_CHAT_TARGET` 默认目标。不明确时回复可选台账名称，保留原需求 30 分钟；对方直接回复产品名称即可继续，回复“取消”结束当前补充。
- 消息及读取位置先原子写入 SQLite。模型结果、路由目标、回复 UUID 和下层任务持久化，重试不会重复调用已成功的模型。飞书消息 UUID 有服务端幂等有效范围，不承诺任意长故障窗口下绝对只发送一次。
- 已建档之后的跨轮补充暂不自动修改旧需求；请修改台账后重新设为待生成方案。片段合并不是无限持续的会话记忆。
- 账号工作流隔离在 `runtime/personal/<账号摘要>/`，各来源队列在其 `sources/` 下，下层交接在其 `handoff/` 下。旧机器人任务不会通过个人账号继续发送。
- 联调时先完成授权并启动程序，再让同事私聊：“产品名称增加导出功能，需要保留格式”。应出现对应台账的新需求和接收确认。下层尚未实现时只会等待交接结果，不编造方案或工期。

需求识别仍可能误判，应结合测试台账检查实际整理结果。自动测试全部使用本地替身，没有读取真实聊天、发送消息或消耗 DeepSeek 额度。

## 实现位置与接口依据

- `shared/feishu/auth/`：用户登录、回调校验、凭据保存及刷新。
- `shared/feishu/personal.py`、`messages.py`：个人会话读取及用户身份回复。
- `agents/speaker_agent/adapters/feishu/personal.py`：消息增量接入。
- `agents/speaker_agent/persistence/personal.py`：收件箱、游标、片段与追问上下文。
- `agents/speaker_agent/orchestration/personal.py`：需求判断、路由和交接既有流程。
- 根目录 `main.py`：统一启动、登录和检查命令。

官方依据：[用户 OAuth 实现](https://github.com/larksuite/lark-openapi-mcp/blob/main/src/auth/provider/oauth.ts)、[枚举个人会话](https://github.com/larksuite/cli/blob/main/skills/lark-im/references/lark-im-chat-list.md)、[获取会话历史消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list)、[用户身份发送消息及权限](https://github.com/larksuite/cli/blob/main/skills/lark-im/references/lark-im-messages-send.md)。
