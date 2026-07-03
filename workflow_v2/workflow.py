import asyncio
from datetime import datetime
from typing import Any

from workflow_v2.component.component_manager import ComponentManager
from workflow_v2.component.selector_component import Branch
from workflow_v2.utils import match_parameters
from workflow_v2.workflow_exceptions import NodeExecutionError, NodeTimeoutError, WorkflowError, WorkflowValidationError
from workflow_v2.workflow_logging_config import NodeLogger, WorkflowContextLogger, WorkflowLogger
from workflow_v2.workflow_state_manager import workflow_state_manager
from workflow_v2.workflow_validator import WorkflowValidator


class WorkflowNode:
    def __init__(self, node_id: str, node_data: dict[str, Any]):
        # 基本信息
        self.id = node_id
        self.data = node_data
        self.title = node_data["data"]["nodeMeta"]["title"]

        # 节点关系
        self.next_nodes = []
        self.previous_nodes = []

        # 配置
        self.timeout = 60  # 超时时间(秒)

        # 实际输入输出
        self.input: dict[str, Any] | None = None
        self.output: dict[str, Any] | None = None

        # 状态管理
        self.is_completed = False
        self.is_executing = False  # 新增：标记节点是否正在执行
        self.execution_lock = asyncio.Lock()  # 新增：节点执行锁

        # 新增：节点状态
        self.status = "waiting"  # 可能的值: "waiting", "executing", "completed", "failed"

        # IO Schema
        self.input_schema = node_data.get("data", {}).get("inputs", {}).get("inputParameters", [])
        self.output_schema = node_data.get("data", {}).get("outputs", [])

        # 分支相关属性
        self.branches: dict[str, Branch] = {}  # port_id -> Branch
        self.is_selector = node_data["type"] == "8" or node_data["type"] == 8
        self.is_intent_classification = node_data["type"] == "22" or node_data["type"] == 22

        self.in_execution_path = False

        # 添加执行时间属性
        self.execution_start_time: float | None = None
        self.execution_end_time: float | None = None

        # 失败原因
        self.failure_reason: str | None = None

    def add_branch_node(self, node: "WorkflowNode", port_id: str):
        """添加分支节点"""
        if port_id not in self.branches:
            self.branches[port_id] = Branch(port_id=port_id, nodes=[], conditions=None)
        self.branches[port_id].nodes.append(node)
        if node not in self.next_nodes:
            self.next_nodes.append(node)


class AsyncWorkflowEngine:
    def __init__(self, workflow_id: str, start_input_values: dict[str, Any] | None = None, **kwargs):
        self.workflow_id = workflow_id
        self.nodes = {}
        self.start_nodes = []
        self.tasks = {}
        self.start_input_values = start_input_values or {}

        workflow_logger = WorkflowLogger()
        self.logger = WorkflowContextLogger(self.workflow_id, workflow_logger.logger)
        self.component_manager = ComponentManager(self.logger, **kwargs)

        # 添加节点执行时间记录字典
        self.node_execution_times = {}

        # 添加第一个失败的节点
        self.first_failed_node: WorkflowNode | None = None

    async def build_graph(self, nodes_data, edges_data):
        self.logger.info("==================================")
        self.logger.info(f"Workflow ID: {self.workflow_id}")
        self.logger.info("Building workflow graph")

        # 首先清除该工作流ID的之前状态记录
        await workflow_state_manager.clear_workflow_state(self.workflow_id)

        # 创建所有节点
        for node_data in nodes_data:
            node = WorkflowNode(node_data["id"], node_data)
            self.nodes[node.id] = node

        # 建立节点间的连接关系
        for edge in edges_data:
            source = self.nodes[edge["sourceNodeID"]]
            target = self.nodes[edge["targetNodeID"]]

            if source.is_selector or source.is_intent_classification:
                if source.is_selector:
                    port_id = edge.get("sourcePortID", "true").lower()  # 默认为 true
                else:
                    port_id = edge.get("sourcePortID", "branch_0")  # 默认为第一个分支

                source.add_branch_node(target, port_id)

                # 对于选择器节点，设置分支的条件配置
                if source.is_selector and port_id != "false":
                    branch_index = 0 if port_id == "true" else int(port_id.split("_")[1])
                    if branch_index < len(source.data["data"]["inputs"]["branches"]):
                        source.branches[port_id].conditions = source.data["data"]["inputs"]["branches"][branch_index]

                # 对于意图识别节点，设置分支的意图配置
                elif source.is_intent_classification and port_id != "default":
                    branch_index = int(port_id.split("_")[1])
                    intents = source.data["data"]["inputs"].get("intents", [])
                    if branch_index < len(intents):
                        source.branches[port_id].conditions = intents[branch_index]
            else:
                # 普通节点的连接
                source.next_nodes.append(target)
            target.previous_nodes.append(source)

        # 3. 找出所有起始节点(入度为0的节点)
        for node in self.nodes.values():
            if not node.previous_nodes:
                self.start_nodes.append(node)

        # 发布初始工作流状态
        asyncio.create_task(self._publish_workflow_init())

    async def _publish_workflow_init(self):
        """发布工作流初始状态"""
        if self.start_nodes:
            start_node = self.start_nodes[0]
            await workflow_state_manager.publish_node_state(workflow_id=self.workflow_id, node_id=start_node.id, node_title=start_node.title, status="waiting")

    async def execute(self):
        self.logger.info("Starting workflow execution")
        try:
            # 重置所有节点的执行路径状态
            for node in self.nodes.values():
                node.in_execution_path = False

            # 为每个起始节点创建任务
            start_tasks = [self.execute_node(node) for node in self.start_nodes]
            # 并行执行所有起始任务
            await asyncio.gather(*start_tasks)

            # 发布工作流完成状态
            await workflow_state_manager.publish_workflow_completed(self.workflow_id)

        except Exception as e:
            self.logger.error(f"Workflow execution failed: {e!s}", exc_info=True)
            raise

    async def execute_node(self, node: WorkflowNode) -> None:
        """执行单个节点"""
        node_logger = NodeLogger(self.logger, node)

        try:
            async with node.execution_lock:
                if node.is_executing or node.is_completed:
                    return
                node.is_executing = True
                node.in_execution_path = True  # 标记当前节点在执行路径上
                node.status = "executing"  # 更新节点状态

                # 发布节点开始执行状态
                await workflow_state_manager.publish_node_state(workflow_id=self.workflow_id, node_id=node.id, node_title=node.title, status="executing")

                task = asyncio.create_task(self._process_node(node))
                self.tasks[node.id] = task
                await task

                if node.is_selector or node.is_intent_classification:  # 添加意图识别判断
                    # 对于选择器，获取选中的端口
                    if node.is_selector:
                        selected_port = node.output.get("selected_port")
                    # 对于意图识别，根据分类ID获取对应的分支
                    else:
                        # 从节点输出中获取 classificationId，如果不存在则默认为 0
                        classification_id = node.output.get("classificationId", 0)

                        # 如果 classification_id = 0，表示默认分支
                        # 如果 classification_id > 0，则使用 branch_{classification_id-1} 作为分支
                        selected_port = "default" if classification_id == 0 else f"branch_{classification_id - 1}"

                    next_tasks = []

                    if selected_port in node.branches:
                        # 标记选中分支上的所有节点
                        for next_node in node.branches[selected_port].nodes:
                            next_node.in_execution_path = True
                            # 修改检查逻辑：只检查在执行路径上的前置节点是否完成
                            if all(prev.is_completed for prev in next_node.previous_nodes if prev.in_execution_path):
                                next_tasks.append(self.execute_node(next_node))

                    if next_tasks:
                        await asyncio.gather(*next_tasks)
                else:
                    next_tasks = []
                    for next_node in node.next_nodes:
                        # 修改检查逻辑：只检查在执行路径上的前置节点是否完成
                        if all(prev.is_completed for prev in next_node.previous_nodes if prev.in_execution_path):
                            next_tasks.append(self.execute_node(next_node))

                    if next_tasks:
                        await asyncio.gather(*next_tasks)

        except Exception as e:
            node_logger.error(f"Node execution failed: {e!s}", exc_info=True)
            node.status = "failed"
            node.failure_reason = str(e)

            # 发布节点失败状态
            await workflow_state_manager.publish_node_state(
                workflow_id=self.workflow_id,
                node_id=node.id,
                node_title=node.title,
                status="failed",
                started_at=node.execution_start_time,
                execution_time=datetime.now().timestamp() - node.execution_start_time if node.execution_start_time else None,
                error_message=str(e),
            )

            if self.first_failed_node is None:
                self.first_failed_node = node
            raise NodeExecutionError(node_id=node.id, node_title=node.title, message=str(e))

    async def _process_node(self, node: WorkflowNode) -> None:
        node_logger = NodeLogger(self.logger, node)
        node_logger.info(f"Start processing node: 【{node.title}】")

        try:
            # 记录开始时间
            node.execution_start_time = datetime.now().timestamp()

            async with asyncio.timeout(node.timeout):
                if node.previous_nodes:
                    active_prev_nodes = [n for n in node.previous_nodes if n.in_execution_path]
                    if active_prev_nodes:
                        await asyncio.gather(*[self.tasks[n.id] for n in active_prev_nodes])

                resolved_inputs = await self._resolve_node_inputs(node)
                component = self.component_manager.create_component(node.data)
                component.inputs = resolved_inputs
                node.input = resolved_inputs
                component.nodes = self.nodes
                component.workflow_node = node

                outputs = await component.execute()
                node.output = outputs

                node.is_completed = True
                node.status = "completed"
                node_logger.info(f"Node 【{node.title}】 completed")

                # 发布节点完成状态
                await workflow_state_manager.publish_node_state(
                    workflow_id=self.workflow_id,
                    node_id=node.id,
                    node_title=node.title,
                    status="completed",
                    started_at=node.execution_start_time,
                    execution_time=datetime.now().timestamp() - node.execution_start_time,
                )

        except TimeoutError:
            node_logger.error("Node execution timed out")
            node.status = "failed"

            # 发布节点超时状态
            await workflow_state_manager.publish_node_state(
                workflow_id=self.workflow_id,
                node_id=node.id,
                node_title=node.title,
                status="failed",
                started_at=node.execution_start_time,
                execution_time=node.timeout,
                error_message=f"节点执行超时，超过{node.timeout}秒",
            )

            raise NodeTimeoutError(node_id=node.id, node_title=node.title, timeout=node.timeout)
        except WorkflowError as e:
            node.error = {"code": e.error_code.value, "message": str(e), "details": e.details}
            node.status = "failed"

            # 发布节点错误状态
            await workflow_state_manager.publish_node_state(
                workflow_id=self.workflow_id,
                node_id=node.id,
                node_title=node.title,
                status="failed",
                started_at=node.execution_start_time,
                execution_time=datetime.now().timestamp() - node.execution_start_time if node.execution_start_time else None,
                error_message=str(e),
            )

            node_logger.error(f"Node failed: {e!s}", exc_info=True)
            raise
        except Exception as e:
            node.status = "failed"

            # 发布节点错误状态
            await workflow_state_manager.publish_node_state(
                workflow_id=self.workflow_id,
                node_id=node.id,
                node_title=node.title,
                status="failed",
                started_at=node.execution_start_time,
                execution_time=datetime.now().timestamp() - node.execution_start_time if node.execution_start_time else None,
                error_message=str(e),
            )

            node_logger.error(f"Node processing failed: {e!s}", exc_info=True)
            raise
        finally:
            # 记录结束时间并计算执行时长
            node.execution_end_time = datetime.now().timestamp()
            execution_time = node.execution_end_time - node.execution_start_time
            self.node_execution_times[node.id] = round(execution_time, 3)
            node.is_executing = False

    async def _resolve_node_inputs(self, node: WorkflowNode) -> dict[str, Any]:
        """解析节点的输入值"""
        # 如果是开始节点，使用start_input_values
        if not node.previous_nodes:
            return self.start_input_values.copy()

        # 处理常规节点的输入
        input_params = node.data.get("data", {}).get("inputs", {}).get("inputParameters", [])
        return match_parameters(input_params, self.nodes)

    async def cleanup(self):
        """清理工作流资源"""
        self.logger.info("Cleaning up workflow resources")
        try:
            # 取消所有未完成的任务
            for task in self.tasks.values():
                if not task.done():
                    task.cancel()
            # 等待任务取消完成
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        except Exception as e:
            self.logger.error(f"Cleanup failed: {e!s}", exc_info=True)
        finally:
            # 重置工作流状态
            self.tasks.clear()
            self.logger.info("Workflow resources cleaned up")


# 修改run_workflow函数，将workflow_id作为返回值的一部分
async def run_workflow(workflow_data, start_input_values=None, workflow_id: str = "test", **kwargs):
    """
    运行工作流

    Args:
        workflow_data: 工作流数据，包含nodes和edges
        start_input_values: 起始输入值
        workflow_id: 工作流ID
        **kwargs: 其他参数

    Returns:
        dict: 包含执行结果和工作流ID
    """
    engine = AsyncWorkflowEngine(workflow_id=workflow_id, start_input_values=start_input_values, **kwargs)

    # 先进行验证
    validation_issues = WorkflowValidator(workflow_data["nodes"], workflow_data["edges"]).validate_all()
    if len(validation_issues) > 0:
        issue_str = "\n".join([issue.format_message() for issue in validation_issues])
        for issue in validation_issues:
            engine.logger.error(f"{issue.format_message()}")
        raise WorkflowValidationError(message=f"工作流构建失败, 请检查验证问题: {issue_str}")

    # 清除该工作流ID的之前状态记录 - 这里也清除一次，确保在构建图之前就已经清除了
    await workflow_state_manager.clear_workflow_state(workflow_id)

    await engine.build_graph(workflow_data["nodes"], workflow_data["edges"])
    try:
        await engine.execute()
        nodes_io = {k: {"input": v.input, "output": v.output} for k, v in engine.nodes.items()}

        # 获取结果
        end_node = next(node for node in engine.nodes.values() if node.data["type"] == "2")

        engine.logger.info("==================================")

        # 返回结果时包含节点执行时间和工作流ID
        return {
            "workflow_id": workflow_id,  # 添加工作流ID
            "nodes_io": nodes_io,
            "end_node_output": end_node.output,
            "node_execution_times": engine.node_execution_times,
        }
    except NodeExecutionError as e:
        failed_nodes: list[str] = [engine.first_failed_node.id] if engine.first_failed_node else []
        # 如果节点IO均为空，则认为是失败节点
        empty_io_nodes = get_empty_io_nodes(engine)
        if empty_io_nodes:
            failed_nodes.extend(empty_io_nodes)

        data = {
            "workflow_id": workflow_id,  # 添加工作流ID
            "nodes_io": {k: {"input": v.input, "output": v.output, "failure_reason": v.failure_reason} for k, v in engine.nodes.items()},
            "node_execution_times": engine.node_execution_times,
            "failed_nodes": failed_nodes,
        }
        e.workflow_exe_data = data
        raise e
    except WorkflowValidationError as e:
        raise e
    except Exception as e:
        raise e
    finally:
        await engine.cleanup()


def get_empty_io_nodes(engine):
    """
    遍历引擎中所有节点，返回输入和输出都为空的节点列表

    参数:
        engine: 包含 nodes 字典的引擎对象

    返回:
        list: 包含所有 input 和 output 都为空的节点 ID 列表
    """
    empty_io_nodes = []

    for node_id, node in engine.nodes.items():
        if node.input is None and node.output is None:
            empty_io_nodes.append(node_id)

    return empty_io_nodes


# 执行工作流
if __name__ == "__main__":

    async def main():
        workflow_data = {
            "nodes": [
                {
                    "id": "100001",
                    "type": "1",
                    "meta": {"position": {"x": -98.49195687295378, "y": 71.10276911084237}, "testRun": {}},
                    "data": {
                        "nodeMeta": {
                            "description": "工作流的起始节点，用于设定启动工作流需要的信息",
                            "icon": "https://lf3-static.bytednsdoc.com/obj/eden-cn/dvsmryvd_avi_dvsm/ljhwZthlaukjlkulzlp/icon/icon-Start.png",
                            "subTitle": "",
                            "title": "开始",
                        },
                        "outputs": [
                            {"type": "string", "name": "BOT_USER_INPUT", "required": False, "description": "用户本轮对话输入内容"},
                            {"type": "string", "name": "test_input", "required": True, "description": ""},
                        ],
                    },
                },
                {
                    "id": "900001",
                    "type": "2",
                    "meta": {"position": {"x": 1677.645973225257, "y": 110.66022295359087}, "testRun": {}},
                    "data": {
                        "nodeMeta": {
                            "description": "工作流的最终节点，用于返回工作流运行后的结果信息",
                            "icon": "https://lf3-static.bytednsdoc.com/obj/eden-cn/dvsmryvd_avi_dvsm/ljhwZthlaukjlkulzlp/icon/icon-End.png",
                            "subTitle": "",
                            "title": "结束",
                        },
                        "inputs": {
                            "terminatePlan": "useAnswerContent",
                            "streamingOutput": False,
                            "inputParameters": [
                                {"name": "BOT_USER_INPUT", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "BOT_USER_INPUT"}}}},
                                {"name": "llm2_1_output", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "158417", "name": "output"}}}},
                                {"name": "llm2_2_output", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "102984", "name": "output"}}}},
                            ],
                            "content": {"type": "string", "value": {"type": "literal", "content": "bot_user_input:{{BOT_USER_INPUT}}\nllm2_1_output:{{llm2_1_output}}"}},
                        },
                    },
                },
                {
                    "id": "123228",
                    "type": "3",
                    "meta": {"position": {"x": 495.5229305844795, "y": 202.10425263285}, "testRun": {}},
                    "data": {
                        "nodeMeta": {
                            "description": "调用大语言模型,使用变量和提示词生成回复",
                            "icon": "https://lf3-static.bytednsdoc.com/obj/eden-cn/dvsmryvd_avi_dvsm/ljhwZthlaukjlkulzlp/icon/icon-LLM.png",
                            "subTitle": "大模型",
                            "title": "大模型2",
                        },
                        "inputs": {
                            "settingOnError": {},
                            "inputParameters": [
                                {"name": "BOT_USER_INPUT", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "BOT_USER_INPUT"}}}}
                            ],
                            "llmParam": [
                                {"name": "temperature", "input": {"type": "float", "value": {"type": "literal", "content": "0.7"}}},
                                {"name": "topP", "input": {"type": "float", "value": {"type": "literal", "content": "0.7"}}},
                                {"name": "maxTokens", "input": {"type": "integer", "value": {"type": "literal", "content": "4096"}}},
                                {"name": "frequencyPenalty", "input": {"type": "float", "value": {"type": "literal", "content": "0"}}},
                                {"name": "responseFormat", "input": {"type": "integer", "value": {"type": "literal", "content": "0"}}},
                                {"name": "modleName", "input": {"type": "string", "value": {"type": "literal", "content": "Doubao-pro-128k/240628"}}},
                                {"name": "modelType", "input": {"type": "integer", "value": {"type": "literal", "content": "834781236"}}},
                                {"name": "generationDiversity", "input": {"type": "string", "value": {"type": "literal", "content": "balance"}}},
                                {"name": "prompt", "input": {"type": "string", "value": {"type": "literal", "content": "请将{{output}}翻译成日文"}}},
                                {"name": "enableChatHistory", "input": {"type": "boolean", "value": {"type": "literal", "content": False}}},
                                {"name": "systemPrompt", "input": {"type": "string", "value": {"type": "literal", "content": ""}}},
                            ],
                        },
                        "outputs": [{"type": "string", "name": "output"}],
                        "version": "3",
                    },
                },
                {
                    "id": "134633",
                    "type": "3",
                    "meta": {"position": {"x": 496.0522338884329, "y": -175.42209048057282}, "testRun": {}},
                    "data": {
                        "nodeMeta": {
                            "description": "调用大语言模型,使用变量和提示词生成回复",
                            "icon": "https://lf3-static.bytednsdoc.com/obj/eden-cn/dvsmryvd_avi_dvsm/ljhwZthlaukjlkulzlp/icon/icon-LLM.png",
                            "subTitle": "大模型",
                            "title": "大模型1",
                        },
                        "inputs": {
                            "settingOnError": {},
                            "inputParameters": [
                                {"name": "user_input", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "BOT_USER_INPUT"}}}},
                                {"name": "test_input", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "test_input"}}}},
                                {"name": "llm_input1", "input": {"type": "string", "value": {"type": "literal", "content": "a"}}},
                            ],
                            "llmParam": [
                                {"name": "temperature", "input": {"type": "float", "value": {"type": "literal", "content": "0.7"}}},
                                {"name": "topP", "input": {"type": "float", "value": {"type": "literal", "content": "0.7"}}},
                                {"name": "maxTokens", "input": {"type": "integer", "value": {"type": "literal", "content": "4096"}}},
                                {"name": "frequencyPenalty", "input": {"type": "float", "value": {"type": "literal", "content": "0"}}},
                                {"name": "responseFormat", "input": {"type": "integer", "value": {"type": "literal", "content": "0"}}},
                                {"name": "modleName", "input": {"type": "string", "value": {"type": "literal", "content": "Doubao-pro-4k/240515"}}},
                                {"name": "modelType", "input": {"type": "integer", "value": {"type": "literal", "content": "834764852"}}},
                                {"name": "generationDiversity", "input": {"type": "string", "value": {"type": "literal", "content": "balance"}}},
                                {"name": "prompt", "input": {"type": "string", "value": {"type": "literal", "content": "请将“{{output}}”翻译成中文"}}},
                                {"name": "enableChatHistory", "input": {"type": "boolean", "value": {"type": "literal", "content": False}}},
                                {"name": "systemPrompt", "input": {"type": "string", "value": {"type": "literal", "content": ""}}},
                            ],
                        },
                        "outputs": [{"type": "string", "name": "output"}],
                        "version": "3",
                    },
                },
                {
                    "id": "158417",
                    "type": "3",
                    "meta": {"position": {"x": 1171.2824616402486, "y": -177.91296045015187}, "testRun": {}},
                    "data": {
                        "nodeMeta": {
                            "description": "调用大语言模型,使用变量和提示词生成回复",
                            "icon": "https://lf3-static.bytednsdoc.com/obj/eden-cn/dvsmryvd_avi_dvsm/ljhwZthlaukjlkulzlp/icon/icon-LLM.png",
                            "subTitle": "大模型",
                            "title": "大模型2_1",
                        },
                        "inputs": {
                            "settingOnError": {},
                            "inputParameters": [
                                {"name": "input", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "BOT_USER_INPUT"}}}},
                                {"name": "llm1_output", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "134633", "name": "output"}}}},
                            ],
                            "llmParam": [
                                {"name": "modelType", "input": {"type": "integer", "value": {"type": "literal", "content": "834781236"}}},
                                {"name": "modleName", "input": {"type": "string", "value": {"type": "literal", "content": "Doubao-pro-128k/240628"}}},
                                {"name": "generationDiversity", "input": {"type": "string", "value": {"type": "literal", "content": "balance"}}},
                                {"name": "temperature", "input": {"type": "float", "value": {"type": "literal", "content": "0.7"}}},
                                {"name": "topP", "input": {"type": "float", "value": {"type": "literal", "content": "0.7"}}},
                                {"name": "maxTokens", "input": {"type": "integer", "value": {"type": "literal", "content": "4096"}}},
                                {"name": "frequencyPenalty", "input": {"type": "float", "value": {"type": "literal", "content": "0"}}},
                                {"name": "responseFormat", "input": {"type": "integer", "value": {"type": "literal", "content": "0"}}},
                                {"name": "prompt", "input": {"type": "string", "value": {"type": "literal", "content": "请将{{output}}翻译成韩文"}}},
                                {"name": "enableChatHistory", "input": {"type": "boolean", "value": {"type": "literal", "content": False}}},
                                {"name": "systemPrompt", "input": {"type": "string", "value": {"type": "literal", "content": ""}}},
                            ],
                        },
                        "outputs": [{"type": "string", "name": "output", "description": ""}],
                        "version": "3",
                    },
                },
                {
                    "id": "102984",
                    "type": "3",
                    "meta": {"position": {"x": 1172.0872118590732, "y": 38.127985744663896}, "testRun": {}},
                    "data": {
                        "nodeMeta": {
                            "description": "调用大语言模型,使用变量和提示词生成回复",
                            "icon": "https://lf3-static.bytednsdoc.com/obj/eden-cn/dvsmryvd_avi_dvsm/ljhwZthlaukjlkulzlp/icon/icon-LLM.png",
                            "subTitle": "大模型",
                            "title": "大模型2_2",
                        },
                        "inputs": {
                            "settingOnError": {},
                            "inputParameters": [
                                {"name": "input", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "100001", "name": "BOT_USER_INPUT"}}}},
                                {"name": "llm1_output", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "134633", "name": "output"}}}},
                                {"name": "llm2_output", "input": {"type": "string", "value": {"type": "ref", "content": {"source": "block-output", "blockID": "123228", "name": "output"}}}},
                            ],
                            "llmParam": [
                                {"name": "modelType", "input": {"type": "integer", "value": {"type": "literal", "content": "834781236"}}},
                                {"name": "modleName", "input": {"type": "string", "value": {"type": "literal", "content": "Doubao-pro-128k/240628"}}},
                                {"name": "generationDiversity", "input": {"type": "string", "value": {"type": "literal", "content": "balance"}}},
                                {"name": "temperature", "input": {"type": "float", "value": {"type": "literal", "content": "0.7"}}},
                                {"name": "topP", "input": {"type": "float", "value": {"type": "literal", "content": "0.7"}}},
                                {"name": "maxTokens", "input": {"type": "integer", "value": {"type": "literal", "content": "4096"}}},
                                {"name": "frequencyPenalty", "input": {"type": "float", "value": {"type": "literal", "content": "0"}}},
                                {"name": "responseFormat", "input": {"type": "integer", "value": {"type": "literal", "content": "0"}}},
                                {"name": "prompt", "input": {"type": "string", "value": {"type": "literal", "content": "请将{{output}}翻译成韩文"}}},
                                {"name": "enableChatHistory", "input": {"type": "boolean", "value": {"type": "literal", "content": False}}},
                                {"name": "systemPrompt", "input": {"type": "string", "value": {"type": "literal", "content": ""}}},
                            ],
                        },
                        "outputs": [{"type": "string", "name": "output", "description": ""}],
                        "version": "3",
                    },
                },
            ],
            "edges": [
                {"sourceNodeID": "158417", "targetNodeID": "900001"},
                {"sourceNodeID": "102984", "targetNodeID": "900001"},
                {"sourceNodeID": "100001", "targetNodeID": "123228"},
                {"sourceNodeID": "100001", "targetNodeID": "134633"},
                {"sourceNodeID": "134633", "targetNodeID": "158417"},
                {"sourceNodeID": "123228", "targetNodeID": "102984"},
                {"sourceNodeID": "134633", "targetNodeID": "102984"},
            ],
        }
        start_input_values = {"BOT_USER_INPUT": "Hello", "test_input": "test"}

        result = await run_workflow(workflow_data, start_input_values=start_input_values)
        print(f"Workflow result: {result}")

    asyncio.run(main())
