---
name: port-ragflow-commit
description: 将指定的 RAGFlow commit 或 PR 评估、移植到 MultiRAG；在用户要求跟进该提交时使用。
---

# RAGFlow 提交跟进

这是 Claude 与 Codex 共用的流程入口；用户本次指令和 [AGENTS.md](../../../AGENTS.md) 优先。
按请求完成评估或移植。移植交付包括行为适配、适用验证、必要文档和明确的未完成项。

## 基准与范围

默认上游 checkout 为本仓兄弟目录 `../ragflow`，可用 `RAGFLOW_REPO` 覆盖。
先确认该 checkout 的 remote 指向预期上游，再 fetch 并记录 `origin/main` SHA；无法刷新时说明基准时点。
`origin/main` 用于检查后续演进，目标代码取用户指定提交的 `git show <commit>:<path>`，不用本地工作树代替。
用户指定其他基准时遵循该基准；默认只移植指定 commit 的行为。

评估覆盖完整 diff 的功能足迹、我方已有接口，以及相关 revert / re-land / 后续修复链。
已回退的改动以有效 re-land 或修复链评估，交代与原请求的差异；必要前置修复纳入说明，
会改变目标或兼容性的范围变化再澄清。已有等价实现复用；不把 WIP、功能回退或假成功桩搬入本仓。

## 按改动选择参考

| 涉及内容 | 阅读与处置 |
|---|---|
| Python 生产代码 | [Python 适配](references/python.md)：框架、async、兼容入口和测试 |
| Go `cmd/`、`internal/` | [Go 适配](references/go.md)：只在本次提交涉及 Go 或用户点名时处理 |
| 上游 `web/` | 前端是独立 `web` 仓，默认另排；用户授权同批时确认实际 checkout，按 API 契约适配我方实现，先核实目标版本与当前上游契约差异 |
| 文档 / CI / 工具链 | 逐项判断本仓是否需要；已有等价设施时不复制上游 harness |
| 相似历史问题 | 按接口或提交号查 [历史判例](references/cases.md)，无需通读 |

## 验证与交付

按 [AGENTS.md 验证表](../../../AGENTS.md#验证) 及所选栈验证，修复本次引入的失败；只读评估与纯文档不触发全套 Python 门禁。
报告采用的上游 SHA、实际行为变化、保留的本地适配、跳过项理由和本次验证证据。
结构性映射变化更新 [RAGFLOW_PORTING_MAP](../../../docs/enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md) 对应项；程序状态按所属账本维护。

用户要求提交时用 conventional commit，标题不提上游来源，不加 `Co-Authored-By`；Go 与 Python 通常分开提交。
暂存前逐 hunk 核对归属，同文件混合修改不能靠关键词猜测归属；`internal/*.md` 不暂存。
不自动写持久记忆；用户明确要求记录记忆时，遵循该环境的记忆写入规则。
