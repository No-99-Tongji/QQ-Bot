# CLAUDE.md

## 架构指引（重要）

- **后端的能力要封装成 mcp server 暴露给 opencode。**
  - 后端（QQ bot）在群里收到「小逸 / 逸浔」等关键词时，转交给服务器上的 opencode（自动模式，`opencode run`）对话。
  - opencode 通过 MCP server 调用后端能力（如 `sing_song` 唱歌/点歌、`show_persona` 人物画像），能力以工具形式暴露给它。
  - 唱歌/画像的结果经 opencode 汇总后由 bot 发回群里（注：本地曲库的语音只能通过发送 `[CQ:record]` 送达，故 `sing_song` 仍需把语音作为副作用发出，文字结果走 opencode 汇总）。
  - 相关代码：`opencode/mcp_server.py`（MCP server）、`opencode.json`（opencode 的 MCP/模型配置）、`plugins/TheresaEason/`（逸浔/小逸 → opencode handoff）。
  - opencode 由 DeepSeek（DPSK key）驱动。

## 日常开发

- 使用 uv 管理依赖（`uv sync`）。
- 插件的运行时真实配置在私有仓库 `qq-bot-configs`，程序只读 `.ini`；`configs/*.toml` 是草稿。
- 部署：改代码 push 到 `No-99-Tongji/QQ-Bot`；改配置 push 到 `No-99-Tongji/qq-bot-configs`；两个仓库 push 都会触发服务器蓝绿自动部署。
- 服务器细节见 `.claude/skills/deploy`。
