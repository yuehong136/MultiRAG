# Canvas 跨轮组件输出

`Canvas.run()` 开始新一轮时，仅清空当前 `path` 中组件的输出。
`UserFillUp` 暂停会把 `path` 替换为待续跑节点；此前完成的组件输出继续保存在
该会话的 DSL 中，供后续表单提示和消息引用使用。续跑不重新执行这些完成节点。
普通消息完整执行后，已执行路径在下一轮清空输出并重新执行；未经过的节点保持原状态。

显式 `Canvas.reset()` 仍清空全部组件的输入、输出和路径，保留既有 `mem` 参数边界。
REST 创建新会话及 completion 首次建会话均调用完整 reset；组件 debug 也先完整 reset，
不会写回会话或 Redis 副本。已有会话从自身 SQL DSL 恢复，普通运行从按 owner 和
runtime user ID 分隔的 Redis 副本恢复。跨用户草稿运行仍受现有权限检查限制。

本项对应上游 `decb5dcb6f25a2be7d92d33277edc444d2cf961b` 的完整单文件改动，
在本地 `db517cd394b33774bf1d1b49f0dc8560303eb00a` 上适配，后修比较上限为
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。这与完整 reset 恢复 env 默认值是两个行为。
采用路径成员判断机制，保留本地 async 执行、取消和终态持久化链。
实际 HTTP 验收还发现：会话完成器在提交暂停状态后才刷新此前的 `message_end`，
JSON 聚合器会因此丢失 `user_inputs` 表单。本地适配让尾随的消息结束保留已收到的暂停表单，
保留现有流式事件次序和完整执行的结束语义。

同链后修处置：

- `c15241c3a9226c1d9d94f213f1c1689e382bd791` 增加新会话清路径：本地新会话已经
  完整 reset，覆盖路径、历史与组件状态；无需叠加新的入口。
- `8a3699fa87d3842e6981d1313216674b86d9d81f` 将非 Begin 的逐轮 reset 扩为输入输出：
  目标提交仅清输出，本项保持此边界。本地 `ComponentBase.get_input()` 对配置引用重新解析，
  Message 的流式输出直接读取当前变量；两次续跑实际消费当轮新表单值。
  该后修涉及其他组件缓存输入的生命周期，不在本项中扩大清理范围。
- 后续 Loop 暂停调度和批次上游依赖修复涉及调度策略，本项不引入。

回归入口：

- `tests/unit/test_canvas_path_reset.py`：真实 Canvas、Begin、UserFillUp、Message，
  覆盖两次暂停及序列化重载、未经过节点、已执行节点的下一轮 reset、完整 reset。
- `tests/integration/test_canvas_path_reset.py`：真实鉴权 HTTP、SQL 会话与 Redis 副本，
  覆盖 JSON/SSE、不同会话及 runtime user ID 交错运行、跨用户拒绝后无状态变化、
  实际调用次数、持久化读回、debug 和显式 reset 边界；复用 scratch 清理读回。

本项无需云模型或外部业务工具。完整组合门禁由批次集成者在最终 SHA 执行。
