# 飞书多 Agent 需求工作流

当前实现最上层 Speaker Agent：接收台账或会话需求、整理需求单、交给下层需求分析与技术评审，再按 Markdown 模板回填原台账并回复需求人。

下层 Agent 尚未实现。Speaker 不自行生成技术方案、工期或风险结论；当前用规则整理字段、保留原文，通过文件交接或内部回调接收下层结果。

## 目录

所有 Agent 都放在 `agents/` 下，后续分析、评审等 Agent 与 `speaker_agent/` 并列。

```text
agents/
  speaker_agent/
    main.py                 # 飞书长连接启动入口
    config.py               # 环境配置
    adapters/feishu/        # 飞书 API、消息与台账事件
    domain/                 # 需求模型、结果契约、外部 I/O 接口
    intake/                 # 台账和聊天内容整理
    orchestration/          # 接收、交接、结果合并与回填
    persistence/            # 持久队列、需求快照、投递检查点
    downstream/             # 下层 Agent 文件交接适配器
    presentation/           # Markdown 格式化
    templates/reply.md      # 需求人回复模板，可直接修改
tests/                      # 本地测试，不连接真实飞书
runtime/                    # 本地运行数据，不提交 Git
```

## 台账字段

当前一个 Speaker 实例绑定一张台账，字段名称可在 `.env` 配置。

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| 需求标题 | 文本 | 需求名称 |
| 状态 | 单选 | 至少包含“待生成方案”“生成中”“已生成”“生成失败” |
| 提出人 | 人员 | 台账来源需求的回复对象，使用 open_id |
| 需求原文 | 多行文本 | 完整保存会话描述，不丢失换行 |
| 处理结论 | 多行文本 | 写入按模板生成的 Markdown 正文 |
| 处理备注 | 多行文本 | 当前等待说明 |
| 方案链接 | 超链接 | 仅开启可选云文档功能时需要 |

已有台账可补充：需求背景、业务目标、功能要求、非功能要求、约束条件、验收标准、优先级、期望上线时间、相关链接。Speaker 会读取这些字段。会话建档只写标题、状态、提出人和原文，避免将自由文本误写入日期、人员或超链接字段。

缺少业务目标、功能要求或验收标准时，需求单中会标出待补充项。未收到完整分析和评审时保持“生成中”；收齐后回填“处理结论”并设为“已生成”。暂时保留初稿中的状态名称以兼容台账，可通过环境配置调整。传输错误保留任务重试，错误原因记入日志及本地任务；目前不自动切换到“生成失败”。

## 飞书配置

1. 创建企业自建应用并启用机器人。
2. 开通多维表格读写、接收消息和以机器人身份回复/发送消息的权限。
3. 长连接订阅 `drive.file.bitable_record_changed_v1` 和 `im.message.receive_v1`。
4. 发布应用，并授予应用目标台账的编辑权限。
5. 群聊需配置 `FEISHU_BOT_OPEN_ID`；仅真正 @此机器人时接收需求。未配置时只接收私聊和台账事件。
6. `FEISHU_NOTIFY_CHAT_ID` 可选；不填则只回复原消息或提出人。
7. 仅在 `FEISHU_CREATE_DOCUMENTS=true` 时，需要额外的云文档创建、编辑和文件权限管理能力。

填写正确的 `FEISHU_BITABLE_APP_TOKEN` 和 `FEISHU_BITABLE_TABLE_ID`；`FEISHU_BITABLE_URL` 可填台账链接。已有记录命令只处理当前配置的台账。

## 启动与检查

```powershell
cd D:\WorkFlows\FeiShuWorkFlows
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# 编辑 .env，填写应用与台账配置
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pyright
.\.venv\Scripts\python.exe -m agents.speaker_agent.main
```

已有 `.venv` 或 `.env` 时直接沿用，不覆盖实际配置。VS Code 解释器选择 `.venv/Scripts/python.exe`。源码、模板、文档和 `.env` 使用 UTF-8；加载配置和模板兼容 UTF-8 BOM。

## 使用

- 台账状态改为“待生成方案”，等待事件触发或默认每 5 分钟补扫。
- 私聊发送 `需求 具体描述`，支持多行和 `业务目标：`、`功能要求：` 等字段标签。
- 群内发送 `@机器人 需求 具体描述`。
- 已有记录发送 `整理需求 记录ID或记录链接`，该记录仍需处于待处理状态。兼容原命令“生成方案”。
- 发送“帮助”查看用法。普通聊天不会全部自动建档；不含明确需求意图时提示使用“需求”命令。

## 下层交接与返回

完整交接说明见 [Speaker 交接协议](agents/speaker_agent/downstream/README.md)。

1. Speaker 将需求快照写到 `runtime/handoff/outbox/<request_id>.json`。
2. 下层需求分析 Agent 写 `runtime/handoff/inbox/<request_id>.analysis.json`，技术评审 Agent 写 `<request_id>.review.json`。两个 Agent 独立写文件，避免覆盖彼此结果。
3. Speaker 每 5 秒检查等待中的任务，合并两类结果。只收到其中一类时继续等待。
4. 收齐后，用 `agents/speaker_agent/templates/reply.md` 生成正文，回填对应台账记录，并回复原消息；无原消息时发送给提出人。

模板使用 `${title}` 等具名占位符。可修改模板内容、顺序和标题，或通过 `SPEAKER_REPLY_TEMPLATE` 指定其他文件；修改后重启生效。聊天和台账当前保存/发送 Markdown 文本，飞书纯文本消息不保证显示 Markdown 富文本效果。

## 当前边界

- 本地单进程版本：持久队列、需求快照和回填/通知检查点保存在 `runtime/speaker.sqlite3`。同一运行目录只启动一个 Speaker 实例。
- 当前支持文本会话和一张台账；不自动抓取任意人的历史聊天，不解析附件、图片或转发消息包。可把聊天内容复制到“需求”消息中。
- 下层结果必须带匹配的 `request_id`；需求内容或状态已变更时，旧结果不会回填。修改后把台账设回待处理状态以发起新一轮。
- 通知失败单独重试，已成功的目标不重复投递。使用飞书的创建幂等键和消息 UUID；不宣称跨任意故障窗口绝对只投递一次。
- 开启可选云文档时，远端创建成功但本地尚未记录文档 ID 就退出，仍可能产生重复文档。默认关闭云文档。
- 暂未接入语言模型、生产队列或真实下层 Agent；目前验证范围为本地模拟测试、SDK 请求构造和静态类型检查，真实租户仍需联调。
