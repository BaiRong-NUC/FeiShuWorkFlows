# 飞书多 Agent 需求工作流

当前实现最上层 Speaker Agent：接收台账或会话需求、整理需求单、交给下层需求分析与技术评审，再按 Markdown 模板回填原台账并回复需求人。

Speaker 默认使用 DeepSeek 理解聊天中的需求意图，整理标题、背景、目标、需求描述、约束、验收信息和待确认问题，同时保留完整原文。需求分析、技术方案、工期和评审结论仍由下层 Agent 返回；下层 Agent 尚未实现。

## 目录

Agent 业务代码放在 `agents/` 下，后续分析、评审等 Agent 与 `speaker_agent/` 并列。大模型调用等可复用基础模块放在 `shared/`；回复模块和业务模板留在各 Agent 下。

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
    templates/              # intake_prompt.md、requirement.md、reply.md
shared/
  llm/                      # 公共 DeepSeek 客户端、模型配置、调用接口和异常
tests/                      # 本地替身测试，不连接真实飞书或 DeepSeek
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
| 需求单 | 多行文本 | 保存 DeepSeek 整理的需求单，名称可用 FEISHU_BRIEF_FIELD 配置 |
| 处理结论 | 多行文本 | 写入按模板生成的 Markdown 正文 |
| 处理备注 | 多行文本 | 当前等待说明 |
| 方案链接 | 超链接 | 仅开启可选云文档功能时需要 |

已有台账可补充：需求背景、业务目标、功能要求、非功能要求、约束条件、验收标准、优先级、期望上线时间、相关链接。Speaker 会读取这些字段。大模型模式的会话建档写标题、状态、提出人、原文和“需求单”正文，避免将自由文本误写入日期、人员或超链接字段。已有台账仍按结构化字段读取，不在轮询和结果回填时重复调用模型。

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

## DeepSeek 配置

在本地 `.env` 中填写以下配置，Key 不写入源码或模板，也不要提交到 Git：

```dotenv
SPEAKER_INTAKE_MODE=deepseek
DEEPSEEK_API_KEY=填入你的Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TIMEOUT_SECONDS=45
DEEPSEEK_MAX_TOKENS=4096
SPEAKER_INTAKE_MAX_CHARS=12000
FEISHU_BRIEF_FIELD=需求单
```

已有台账需要新增“需求单”多行文本列，或将 `FEISHU_BRIEF_FIELD` 指向独立的已有文本列。不要与“需求原文”“处理结论”复用同一列。

公共客户端使用 DeepSeek Chat Completions 接口，开启 JSON 输出并禁用思考模式；默认模型按 [DeepSeek 官方模型文档](https://api-docs.deepseek.com/quick_start/pricing/) 配置为 `deepseek-flash`。协议细节参考 [JSON 输出说明](https://api-docs.deepseek.com/guides/json_mode/)。实现使用 Python 标准库，无需安装 OpenAI SDK。

- 启用后，收到的聊天文本会发送到配置的 DeepSeek 接口；不发送飞书凭据或会话路由 ID。
- 缺少 Key 时启动直接报错，不自动降级为规则模式。需要离线规则整理时明确设置 `SPEAKER_INTAKE_MODE=rules`。
- 模型结果必须通过字段白名单、类型和长度校验。输出不能包含评审结论、工期、人员 ID 或回填目标。
- 整理结果先存入本地 SQLite，再新建台账。写表重试、轮询和重启复用已保存结果；模型响应返回后、结果尚未保存就退出的极小窗口仍可能重复请求。
- 网络或无效输出最多尝试 3 次，失败重试间隔由持久队列控制，默认为 30 秒；鉴权、余额、参数等不可重试错误立即停止。最终失败会回复“尚未新建台账”，修正配置后重新发送消息即可。
- 不凭空补全缺失信息；未明确提供的验收、日期等留空，并生成待确认问题。当前缺失字段与问题会随需求单交给下层。

## 启动与检查

```powershell
cd D:\WorkFlows\FeiShuWorkFlows
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# 编辑 .env，填写飞书配置和 DEEPSEEK_API_KEY
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pyright
.\.venv\Scripts\python.exe -m agents.speaker_agent.main
```

已有 `.venv` 或 `.env` 时直接沿用，不覆盖实际配置。VS Code 解释器选择 `.venv/Scripts/python.exe`。源码、模板、文档和 `.env` 使用 UTF-8；加载配置和模板兼容 UTF-8 BOM。

## 使用

- 台账状态改为“待生成方案”，等待事件触发或默认每 5 分钟补扫。
- 私聊可直接描述，例如“月底财务总要一个个复制订单金额，给个按月份导出的入口呗”。不再要求固定关键词，也支持 `需求 具体描述` 和多行聊天转录。
- 群内先 @机器人，再直接描述需求。
- 已有记录发送 `整理需求 记录ID或记录链接`，该记录仍需处于待处理状态。兼容原命令“生成方案”。
- 发送“帮助”查看用法。问候、感谢等由模型识别为非需求时，只提示补充描述，不新建台账。

## 下层交接与返回

完整交接说明见 [Speaker 交接协议](agents/speaker_agent/downstream/README.md)。

1. Speaker 将需求快照写到 `runtime/handoff/outbox/<request_id>.json`。
2. 下层需求分析 Agent 写 `runtime/handoff/inbox/<request_id>.analysis.json`，技术评审 Agent 写 `<request_id>.review.json`。两个 Agent 独立写文件，避免覆盖彼此结果。
3. Speaker 每 5 秒检查等待中的任务，合并两类结果。只收到其中一类时继续等待。
4. 收齐后，用 `agents/speaker_agent/templates/reply.md` 生成正文，回填对应台账记录，并回复原消息；无原消息时发送给提出人。

Speaker 的三个模板均在 `agents/speaker_agent/templates/`：`intake_prompt.md` 约束模型的需求理解职责，`requirement.md` 控制整理后的需求单格式，`reply.md` 控制下层结果的对外回复格式。

需求单和回复模板使用 `${title}` 等具名占位符。可修改内容、顺序和标题，或通过 `SPEAKER_REPLY_TEMPLATE` 指定其他回复模板；修改后重启生效。聊天和台账当前保存/发送 Markdown 文本，飞书纯文本消息不保证显示 Markdown 富文本效果。

## 当前边界

- 本地单进程版本：持久队列、需求快照和回填/通知检查点保存在 `runtime/speaker.sqlite3`。同一运行目录只启动一个 Speaker 实例。
- 当前支持文本会话和一张台账；不自动抓取任意人的历史聊天，不解析附件、图片或转发消息包。可把聊天内容复制到“需求”消息中。
- 每条提交的消息独立整理，可在一条消息中粘贴多轮聊天；暂未自动合并会话前后文，也不把后续补充自动关联到旧需求。需要补充时可更新台账字段并重新触发。
- 下层结果必须带匹配的 `request_id`；需求内容或状态已变更时，旧结果不会回填。修改后把台账设回待处理状态以发起新一轮。
- 通知失败单独重试，已成功的目标不重复投递。使用飞书的创建幂等键和消息 UUID；不宣称跨任意故障窗口绝对只投递一次。
- 开启可选云文档时，远端创建成功但本地尚未记录文档 ID 就退出，仍可能产生重复文档。默认关闭云文档。
- DeepSeek 接入代码已实现，测试使用本地替身，未消耗真实 API 额度；没有实际 Key 的在线验证。生产队列和真实下层 Agent 尚未接入，飞书租户仍需联调。
