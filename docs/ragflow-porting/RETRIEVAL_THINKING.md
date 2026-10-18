# 检索进度与模型思考

知识库聊天的 deep research 进度使用独立的 `<retrieving>…</retrieving>` 包裹，
模型思考继续使用 `<think>…</think>`（流式传输也支持既有的
`start_to_think` / `end_to_think` 标志）。两者不能共用思考状态。

`api/db/services/dialog_service.py` 的同步与异步聊天共用
`_deep_research_event_payload` 转换回调。流式进度事件为增量，`final=False`；
最终装饰答案包含完整检索块前缀，检索内容不参与模型思考归一化或正文引用插入。
这样会话持久化、最终 SSE 与历史接口读回保留同一边界。非流式请求只返回最终答案，
不输出中间检索事件。后台研究任务的关闭、取消和异常继续由共用事件桥接器管理。

独立 Web 仓的共享解析器为实时和历史内容提取检索、思考和答案，保留正文前后的文字、
多个过程块和引用。标签可以跨 SSE 帧；只有匹配的显式闭标签结束过程块，
换段、EOF、完成或取消不能把未闭合过程的后续段落提升成正文。
代码、数学公式、转义文字及 HTML 中的标签文本保持原有内容边界，不做全局反转义或实体解码。

首页聊天、应用聊天（含 Studio 复用组件）、Search 摘要与 Agent 分享/widget 消息列表
使用独立的检索和思考折叠控件，默认折叠，流式期间也可通过键盘或鼠标操作。
检索过程具有中英文进行中、完成和中断标题；停止接收后保留检索原文并结束忙碌状态。
过程正文以 React 文本显示，检索回调的换行标签仅转换成换行，不注入 HTML。
正文继续通过现有 XMarkdown、引用组件和公式扩展呈现。

回归入口：

- `tests/unit/test_dialog_web_search_toggle.py`：进度信封、同步/异步最终答案及会话消息构造、非流式边界、桥接关闭/异常。
- `tests/unit/test_dialog_final_answer.py`、`tests/unit/test_conversation_stream.py`：思考流、装饰正文和兼容累积 SSE。
- `tests/integration/test_chat_session_compat.py`：隔离 HTTP SSE、独立 SQL 与历史接口读回、引用和资源清理。模型输出受控，不证明真实模型或生产运行。
