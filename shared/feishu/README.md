# 公共飞书模块

本包封装可被多个 Agent 使用的飞书能力，不依赖 `agents/`，不包含需求台账的字段名、处理状态或消息回复模板。

| 模块 | 职责 |
| --- | --- |
| `config.py` | 应用凭据与表格地址配置；凭据支持现有 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 环境变量 |
| `client.py` | 创建或接收 SDK Client，组合表格、消息和文档服务 |
| `bitable.py` | 读取、分页查询、新建、更新记录；表内字段 ID 映射与缓存 |
| `catalog.py` | 从文件链接识别类型、解析知识库节点、枚举电子表格工作表和多维表格数据表 |
| `sheets.py` | 普通电子表格的单元格读取、批量写入、追加行及下拉规则读取；复用 SDK 鉴权 |
| `messages.py` | 发送、回复文本；保留消息 UUID 幂等参数 |
| `auth/` | OAuth 登录、PKCE/state 校验、用户 Token 刷新及 Windows DPAPI 加密存储 |
| `personal.py` | 用户身份枚举个人单聊、分页读消息及提取文本；`PersonalChatAPI` 和 Token Provider 接口 |
| `documents.py` | 创建 Markdown 云文档、转换和写入内容、授予查看权限 |
| `connection.py` | 构造事件分发器和长连接客户端，调用方决定订阅与启动时机 |
| `events.py`、`fields.py` | 解析事件字段、处理提及标记、提取字段文本和人员 ID |
| `contracts.py` | `BitableRecord` 数据类型及 `BitableAPI`、`MessageAPI`、`DocumentAPI`、`FeishuServices` Protocol |
| `errors.py` | API 失败、空响应、SDK 服务未初始化时的统一错误与类型收窄 |

```python
from shared.feishu.client import FeishuClient
from shared.feishu.config import BitableConfig, FeishuConfig

# 调用方先加载自己的环境配置；实例化不会发送消息或修改台账。
feishu = FeishuClient(FeishuConfig.from_env())
table = feishu.table(BitableConfig(app_token="你的表格应用Token", table_id="你的表ID"))
# table.get_record(record_id)、table.list_records(filter_expression=...)：读取记录。
# table.create_record(fields, client_token=...)、table.update_record(record_id, fields)：写入任意业务字段。
# feishu.messages、feishu.documents：消息与云文档服务。
# feishu.catalog.discover(("https://example.feishu.cn/wiki/节点Token",))：只读枚举文件内全部表。
# feishu.sheets.read_range(文件Token, 工作表ID)：读取有效区域，不解释业务字段。
```

一个客户端可以创建多个表格服务，每个表格对象独立保存地址和字段缓存；调用方应复用该对象。字段值保持原始结构，不自动压缩多行原文。应用凭据、表格地址可直接构造配置对象，无需依赖 Speaker 的配置类。

`FeishuClient(config, user_tokens=oauth)` 将消息发送/回复绑定到用户身份；表格和云文档仍使用应用身份。个人消息请求显式限定 `AccessTokenType.USER`，不会在授权失败时降级为机器人。`PersonalChatClient(feishu.client, oauth)` 负责用户会话读取；传入自建 SDK Client 时需启用 `enable_set_token(True)`。消息分页和权限错误会抛出异常，不将不完整分页当作成功。

授权文件保存在调用方指定的 `runtime/auth/feishu-user.dat`，与应用绑定；Windows 用当前用户 DPAPI 加密，其他系统使用权限为 0600 的原子文件。刷新期间持有线程锁和进程锁，旋转前记录状态；断网或崩溃导致结果不确定时要求重新授权，不重复消费一次性刷新凭据。SDK 及 OAuth 请求超时为 30 秒；不记录凭据或 OAuth 错误响应正文。

Speaker 自己的网关负责将通用记录转换成需求模型、选择业务列、决定完成状态和链接文案；事件适配器负责选择目标台账、判断状态变化和群聊触发条件。回复模板仍放在 `agents/speaker_agent/templates/`。

测试可注入实现 `FeishuServices` 的本地替身，无需伪装为 SDK Client。SDK 请求构造测试则注入 Mock，覆盖分页、空响应、消息幂等参数和文档写入，不连接真实飞书。电子表格只读 GET 遇到 95203 时最多重试两次；写请求不直接重放，由业务层负责持久化和幂等重试。云文档创建后写入失败仍可能遗留空文档。

多文件编排使用扩展的 `WorkspaceServices` Protocol，包含 `CatalogAPI` 和 `SheetsAPI`。调用这些服务不会自动处理需求；电子表格的 ID 校验、列映射和状态流转在 Speaker 适配器内完成。知识库节点识别参考 [获取知识空间节点信息](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/get_node)，电子表格单元格读取参考 [读取单个范围](https://open.feishu.cn/document/server-docs/docs/sheets-v3/data-operation/reading-a-single-range)。
