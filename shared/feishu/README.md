# 公共飞书模块

本包封装可被多个 Agent 使用的飞书能力，不依赖 `agents/`，不包含需求台账的字段名、处理状态或消息回复模板。

| 模块 | 职责 |
| --- | --- |
| `config.py` | 应用凭据与表格地址配置；凭据支持现有 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 环境变量 |
| `client.py` | 创建或接收 SDK Client，组合表格、消息和文档服务 |
| `bitable.py` | 读取、分页查询、新建、更新记录；表内字段 ID 映射与缓存 |
| `messages.py` | 发送、回复文本；保留消息 UUID 幂等参数 |
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
```

一个客户端可以创建多个表格服务，每个表格对象独立保存地址和字段缓存；调用方应复用该对象。字段值保持原始结构，不自动压缩多行原文。应用凭据、表格地址可直接构造配置对象，无需依赖 Speaker 的配置类。

Speaker 自己的网关负责将通用记录转换成需求模型、选择业务列、决定完成状态和链接文案；事件适配器负责选择目标台账、判断状态变化和群聊触发条件。回复模板仍放在 `agents/speaker_agent/templates/`。

测试可注入实现 `FeishuServices` 的本地替身，无需伪装为 SDK Client。SDK 请求构造测试则注入 Mock，覆盖分页、空响应、消息幂等参数和文档写入，不连接真实飞书。公共模块不隐式重试，业务层负责持久化和重试；云文档创建后写入失败仍可能遗留空文档。
