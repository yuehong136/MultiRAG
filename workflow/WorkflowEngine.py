from dataclasses import dataclass
from typing import Dict, Any, Optional

from fastapi import UploadFile

from workflow.WorkflowContext import WorkflowContext, NodeIOData
from workflow.basic.Node import Node, ValueTypeOfIODefinition, RefContentOfInputDefinition
from workflow.basic.StartNode import StartNode, StartNodeParam, StartNodeInputDefinition
from workflow.components.FileReaderComponent import FileReaderComponentInputDefinition, \
    FileReaderComponentOutputDefinition, FileReaderComponentParam, FileReaderComponent
from workflow.components.FileSelectionComponent import FileSelectionComponent, FileSelectionComponentParam, \
    FileSelectionComponentOutputDefinition
from workflow.components.LLMComponent import LLMComponent, LLMComponentParam, LLMComponentInputDefinition, \
    LLMComponentOutputDefinition

from typing import Dict, Any


class WorkflowEngine:
    def __init__(self, node_list: Optional[list[Node]] = None):
        self.nodes = {}
        self.edges = []
        self.node_list = node_list if node_list is not None else []
        self.context = WorkflowContext()

    def add_node(self, node: Node):
        pass

    def add_node_to_list(self, node: Node):
        self.node_list.append(node)

    def add_edge(self, source_id: str, target_id: str):
        self.edges.append((source_id, target_id))

    async def execute(self, input_data: Optional[dict] = None) -> Dict[str, Any]:
        self.context.clear()  # 清除之前可能存在的上下文数据
        for i in range(len(self.node_list)):
            node = self.node_list[i]

            output_data = await node.process(input_data=input_data, context=self.context)
            if output_data is not None:
                self.context.set(node.node_id, NodeIOData(output_data=output_data))

        return {"node_list": self.node_list, "context": self.context}


if __name__ == "__main__":
    workflowEngine = WorkflowEngine()

    startNodeInputDefinition = StartNodeInputDefinition("USER_INPUT", "string", True, "user input")
    startNodeParam = StartNodeParam([startNodeInputDefinition])
    startNode = StartNode(startNodeParam)
    workflowEngine.add_node_to_list(startNode)

    fileSelectionComponentOutputDefinition = FileSelectionComponentOutputDefinition("FILE_PATH")
    fileSelectionComponentParam = FileSelectionComponentParam(output_definition=fileSelectionComponentOutputDefinition)
    fileSelectionComponent = FileSelectionComponent(fileSelectionComponentParam, "100002")
    workflowEngine.add_node_to_list(fileSelectionComponent)

    fileReaderComponentInputDefinition = FileReaderComponentInputDefinition(parameter_name="FILE_PATH",
                                                                            value_type=ValueTypeOfIODefinition.REF,
                                                                            content=["100002", "FILE_PATH"])
    fileReaderComponentOutputDefinition = FileReaderComponentOutputDefinition("FILE_CONTENT")
    fileReaderComponentParam = FileReaderComponentParam(output_definition=fileReaderComponentOutputDefinition,
                                                        input_definition=fileReaderComponentInputDefinition)
    fileReaderComponent = FileReaderComponent(fileReaderComponentParam, "100004")
    workflowEngine.add_node_to_list(fileReaderComponent)

    llmComponentInputDefinition = LLMComponentInputDefinition(parameter_name="FILE_CONTENT",
                                                              value_type=ValueTypeOfIODefinition.LITERAL,
                                                              content=["100004", "FILE_CONTENT"])
    llmComponentOutputDefinition = LLMComponentOutputDefinition(variable_name="LLM_OUTPUT", variable_type="string",
                                                                description="llm output")
    prompt = """
    你是一位智能的文档判断专家，主要工作就是根据我传输给你的文本内容来确定这份文档的秘密级别，有三种秘密级别：秘密、机密，绝密。
文档内容：
----
{{FILE_CONTENT}}
----
    """
    llmComponent = LLMComponent(LLMComponentParam(model="ep-20240808173556-h7vxq", prompt=prompt,
                                                  output_definition=llmComponentOutputDefinition,
                                                  input_definition_list=[llmComponentInputDefinition]), "100003")
    workflowEngine.add_node_to_list(llmComponent)

    workflowEngine.execute()
