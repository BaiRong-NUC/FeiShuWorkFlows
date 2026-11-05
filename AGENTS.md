# 项目约定

- 所有 Agent 的代码均放在 `agents/<agent_name>/`，当前最上层为 `agents/speaker_agent/`。
- 每个 Agent 按功能拆分子包；接入、领域契约、需求整理、编排、持久化和展示不要混放在根目录。
- 各 Agent 的 Markdown 模板放在自身的 `templates/` 文件夹。Speaker 的回复模板是 `agents/speaker_agent/templates/reply.md`。
- Speaker 负责接收和整理需求、交接下层、汇总结果、回填和回复。技术方案、工期和分析结论必须来自下层返回，不自行编造。
- 源码、文档、模板和示例配置使用 UTF-8 与可读中文；不要把中文和引号写成字面量 Unicode 转义。
- 业务编排依赖公共 Protocol 接口，真实 SDK 和测试替身实现相同契约；正确检查 SDK 的可空值，不关闭类型诊断来隐藏问题。
- 自动测试使用本地替身，不发送真实飞书消息、不修改真实台账。
- 修改后运行 `.venv/Scripts/python.exe -m unittest discover -s tests -v` 和 `.venv/Scripts/python.exe -m pyright`，并检查 `git diff --check`。
