---
name: port-ragflow-commit
description: 跟进或移植一个 ragflow 上游 commit 到 MultiRAG。当用户给出 ragflow commit hash 或 PR 号并要求跟进、移植或对齐上游时使用。
---

# port-ragflow-commit

这是 Codex 的项目级技能入口。完整且权威的工作流保留在 Claude 与 Codex 共用的源文件中，避免两份流程独立演进：

`../../../.claude/skills/port-ragflow-commit/SKILL.md`

使用本技能时，必须先完整读取上述 `SKILL.md`，再执行任何任务操作，并遵循其中的基准选择、栈判定、框架映射、验证、提交和判例约定。

如源技能与当前仓库根目录的 `AGENTS.md` 或用户本轮明确指令冲突，以用户指令和 `AGENTS.md` 为准。
