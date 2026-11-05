# 个人飞书私聊接入

目标：别人直接私聊当前用户的个人飞书账号，Speaker 从对话中识别需求、补充上下文、整理需求单，交给下层分析和评审，并把返回结果按模板发回原会话、写入对应台账。

当前状态：已实现的消息入口是应用机器人 `im.message.receive_v1` 事件。它不等于订阅当前用户的所有个人私聊；仅配置应用凭据并启动长连接不能实现上述目标。现有代码不读取个人私聊，也不以个人身份发送消息。

## 已核实的能力

- App ID / App Secret 仍用于标识应用、申请应用访问凭证及参与用户 OAuth 授权流程，不能因使用个人聊天就删除。
- [获取会话历史消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list) 支持 `user_access_token`。以用户身份读取单聊，需要基础消息读取权限，以及 `im:message.p2p_msg:get_as_user`。需要应用获准权限并由用户授权，单有应用密钥不够。
- 历史消息 API 需要明确的 `chat_id`；它本身不提供“自动监听所有个人私聊”。个人会话发现、增量读取与可用事件需要另行核实。
- 个人身份发送不能沿用当前机器人回复方式。官方 [机器人对外共享限制](https://open.feishu.cn/document/develop-robots/add-bot-to-external-group?lang=zh-CN) 将 `im:message.send_as_user` 列为内测中的权限；本企业、本应用是否能申请和调用仍需验证。不能仅凭已有 Token 就认定可以自动代发。

## 后续接入边界

1. `shared/feishu/`：用户登录授权、Token 安全保存与刷新、个人会话读取和符合授权范围的发送接口。
2. `agents/speaker_agent/adapters/`：将已授权的消息来源转为统一的输入，保留会话、发送者、消息 ID，过滤自己发出的消息，避免循环处理。
3. `agents/speaker_agent/intake/`：按会话汇总上下文，区分闲聊、需求、已有需求补充，支持追问；不能让每句对话都生成一条新需求。
4. 编排与持久化：消息去重、来源表选择、需求与原会话关联、下层结果回填和回复。

个人入口尚未实现。当前测试个人私聊不会触发流程；可先用台账或机器人入口验证 Speaker 的整理与下层交接。所有自动测试仍使用本地替身，不抓取真实个人聊天、不发送真实消息。
