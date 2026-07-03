import json

from workflow.basic.Edge import Edge
from workflow.basic.EndNode import EndNode
from workflow.basic.Node import Node
from workflow.components.ExcelGeneratorComponent import ExcelGeneratorComponent
from workflow.components.FileReaderComponent import FileReaderComponent
from workflow.components.LLMComponent import LLMComponent
from workflow.components.MinIOSelectionComponent import MinIOSelectionComponent
from workflow.components.UserFileSelectionComponent import UserFileSelectionComponent
from workflow.WorkflowEngine import WorkflowEngine

class_map = {
    "UserFileSelectionComponent": UserFileSelectionComponent,
    "FileReaderComponent": FileReaderComponent,
    "LLMComponent": LLMComponent,
    "EndNode": EndNode
}


class WorkflowParser:
    @staticmethod
    def parse(workflow_json_str: str) -> WorkflowEngine:
        workflow_json = json.loads(workflow_json_str)
        node_id_ordered_list = WorkflowParser.get_ordered_nodes(workflow_json)

        node_json_list: list[dict] = []
        for node in workflow_json['nodes']:
            node_id = node['id']
            node_json_list.append({'id': node_id, 'node': node})

        # 创建一个字典，将 node_id 映射到其在 node_id_ordered_list 中的索引
        node_order = {node_id: index for index, node_id in enumerate(node_id_ordered_list)}

        # 使用 sorted() 函数对 node_list 进行排序
        sorted_node_json_list = sorted(node_json_list, key=lambda x: node_order[str(x['id'])])

        node_list: list[Node] = []
        for node_json in sorted_node_json_list:
            node_data = node_json['node']['data']
            name = node_data['componentName']
            if name == "UserFileSelectionComponent":
                userFileSelectionComponent = UserFileSelectionComponent.decode(node_json)
                node_list.append(userFileSelectionComponent)
            elif name == "FileReaderComponent":
                fileReaderComponent = FileReaderComponent.decode(node_json)
                node_list.append(fileReaderComponent)
            elif name == "LLMComponent":
                llmComponent = LLMComponent.decode(node_json)
                node_list.append(llmComponent)
            elif name == "EndNode":
                endNode = EndNode.decode(node_json)
                node_list.append(endNode)
            elif name == "MinIOSelectionComponent":
                minIOSelectionComponent = MinIOSelectionComponent.decode(node_json)
                node_list.append(minIOSelectionComponent)
            elif name == "ExcelGeneratorComponent":
                excelGeneratorComponent = ExcelGeneratorComponent.decode(node_json)
                node_list.append(excelGeneratorComponent)
            else:
                raise ValueError(f"Unknown component name: {name}")

        return WorkflowEngine(node_list)

    @staticmethod
    def get_ordered_nodes(workflow_json: json) -> list[str]:
        edges = [Edge(edge['source'], edge['target']) for edge in workflow_json['edges']]

        graph = {}
        for edge in edges:
            graph[edge.source] = edge.target

        start_node = next(node for node in {edge.source for edge in edges}
                          if node not in {edge.target for edge in edges})

        ordered_nodes = [start_node]
        current_node = start_node
        while current_node in graph:
            next_node = graph[current_node]
            ordered_nodes.append(next_node)
            current_node = next_node

        return ordered_nodes


if __name__ == "__main__":
    # {
    #   "nodes": [
    #     {
    #       "id": 100001,
    #       "data": {
    #         "componentParam": {
    #           "input_definition": {
    #             "variable_name": "FILE",
    #             "variable_type": "File"
    #           }
    #         },
    #         "componentName": "UserFileSelectionComponent"
    #       }
    #     },
    #     {
    #       "id": 100002,
    #       "data": {
    #         "componentParam": {
    #           "input_definition": {
    #             "parameter_name": "INPUT",
    #             "value_type": "REF",
    #             "content": {
    #               "node_id": 100001,
    #               "name": "FILE"
    #             }
    #           },
    #           "output_definition": {
    #             "variable_name": "OUTPUT",
    #             "variable_type": "Object",
    #             "description": "",
    #             "schema": [
    #               {
    #                 "type": "string",
    #                 "name": "fileName"
    #               },
    #               {
    #                 "type": "string",
    #                 "name": "fileContent"
    #               }
    #             ]
    #           }
    #         },
    #         "componentName": "FileReaderComponent"
    #       }
    #     },
    #     {
    #       "id": 100003,
    #       "data": {
    #         "componentParam": {
    #           "input_definition": [
    #             {
    #               "parameter_name": "fileName",
    #               "value_type": "REF",
    #               "content": {
    #                 "node_id": 100002,
    #                 "name": "OUTPUT.fileName"
    #               }
    #             },
    #             {
    #               "parameter_name": "fileContent",
    #               "value_type": "REF",
    #               "content": {
    #                 "node_id": 100002,
    #                 "name": "OUTPUT.fileContent"
    #               }
    #             }
    #           ],
    #           "output_definition": {
    #             "variable_name": "OUTPUT",
    #             "variable_type": "String",
    #             "description": "输出"
    #           },
    #           "model": "ep-20240808171931-wqpv5",
    #           "prompt": "角色：你是一位经验丰富的保密专家，负责评估文档的机密等级。\n任务：仔细分析给定的文档内容，并根据内容的敏感度确定其涉密级别（秘密、机密或绝密）。\n背景：\n* 秘密：泄露可能对国家安全和利益造成损害\n* 机密：泄露可能对国家安全和利益造成严重损害\n* 绝密：泄露可能对国家安全和利益造成特别严重损害\n评估标准：\n1. 信息敏感度\n2. 潜在影响范围\n3. 泄露后果的严重性\n4. 信息的时效性\n5. 涉及的领域（如军事、外交、经济等）\n输出格式：\n1. 涉密级别：[秘密/机密/绝密]\n注意事项：\n* 始终保持谨慎和保守的态度\n* 如遇模糊情况，倾向选择更高级别\n* 不要在回复中重复或提及具体的敏感信息\n* 只需要返回涉密级别，不需要其他内容\n请根据以上指导对下面的文档内容进行分析和定密：\n{{fileContent}}"
    #         },
    #         "componentName": "LLMComponent"
    #       }
    #     },
    #     {
    #       "id": 100004,
    #       "data": {
    #         "componentParam": {
    #           "content": "文件：{{fileName}}\n密级：{{llmOutput}}",
    #           "input_definition": [
    #             {
    #               "parameter_name": "fileName",
    #               "value_type": "REF",
    #               "content": {
    #                 "node_id": 100002,
    #                 "name": "OUTPUT.fileName"
    #               }
    #             },
    #             {
    #               "parameter_name": "llmOutput",
    #               "value_type": "REF",
    #               "content": {
    #                 "node_id": 100003,
    #                 "name": "OUTPUT"
    #               }
    #             }
    #           ]
    #         },
    #         "componentName": "EndNode"
    #       }
    #     }
    #   ],
    #   "edges": [
    #     {
    #       "source": "100003",
    #       "target": "100004"
    #     },
    #     {
    #       "source": "100002",
    #       "target": "100003"
    #     },
    #     {
    #       "source": "100001",
    #       "target": "100002"
    #     }
    #   ]
    # }

    # 读取文件
    workflow_json_str = ""
    with open('/Users/naimehao/Desktop/workflowSampleData.json') as f:
        workflow_json_str = f.read()
    workflowEngine = WorkflowParser.parse(workflow_json_str=workflow_json_str)
