from dataclasses import dataclass
import json

from api.db import LLMType
from api.db.services.llm_service import TenantLLMService, LLMBundle
from workflow.WorkflowContext import WorkflowContext, NodeIOData
from workflow.basic.Component import Component, ComponentParameter
from workflow.basic.Node import ValueTypeOfIODefinition, Batch
from workflow.utils.utils import safe_format_double_braces
from jsonpath_ng import parse


@dataclass
class LLMComponentInputDefinition:
    parameter_name: str
    value_type: ValueTypeOfIODefinition
    content: list[str] | str


@dataclass
class LLMComponentOutputDefinition:
    variable_name: str
    variable_type: str | None = None
    description: str | None = None
    schema: 'LLMComponentOutputDefinition' = None


@dataclass
class LLMComponentParam(ComponentParameter):
    model: str
    prompt: str
    output_definition: LLMComponentOutputDefinition | None = None
    input_definition_list: list[LLMComponentInputDefinition] | None = None
    is_batch: bool = False
    batch: list[Batch] | None = None


class LLMComponent(Component[LLMComponentParam]):
    def __init__(self, component_parameter: LLMComponentParam, node_id: str):
        self.name = "LLMComponent"
        self.node_id = node_id
        self.component_parameter = component_parameter
        super().__init__(component_parameter, node_id)

    async def process(self, input_data: dict | None = None, context: WorkflowContext | None = None,
                      **kwargs) -> dict:
        user = kwargs.get('user')
        db = kwargs.get('db')
        model = self.component_parameter.model
        api_key = TenantLLMService.get_api_key(db, user.id, model).api_key
        prompt = self.component_parameter.prompt

        if self.component_parameter.is_batch:
            parameter_dict = {}
            for input_definition in self.node_parameter.input_definition_list:
                batch_value_list = []
                parameter_name = input_definition['parameter_name']
                if input_definition['value_type'] == ValueTypeOfIODefinition.REF.value:
                    ref_node_id = input_definition['content'][0]
                    ref_name = input_definition['content'][1]
                    ref_node_data = context.get(str(ref_node_id)).output_data
                    for item in ref_node_data[ref_name]:
                        batch_value_list.append(item[input_definition['content'][2]])
                    parameter_dict[parameter_name] = batch_value_list
                elif input_definition.value_type == ValueTypeOfIODefinition.LITERAL:
                    pass

            parameter_dict_list = self.combine_dict_arrays(parameter_dict)
            output_data_list = []
            for parameter_dict in parameter_dict_list:
                chat_mdl = LLMBundle(db, user.id, LLMType.CHAT, model)
                history = [{"role": "user", "content": safe_format_double_braces(prompt, **parameter_dict)}]
                response = chat_mdl.chat(system="", history=history, gen_conf={})
                output_data_list.append(response)
            context.set(str(self.node_id),
                        NodeIOData(output_data={
                            self.component_parameter.output_definition['variable_name']: {
                                self.component_parameter.output_definition['schema'][0]["name"]: output_data_list}}))
        else:
            parameter_dict = {}
            for input_definition in self.node_parameter.input_definition_list:
                parameter_name = input_definition['parameter_name']
                if input_definition['value_type'] == ValueTypeOfIODefinition.REF.value:
                    ref_node_id = input_definition['content'][0]
                    ref_name = input_definition['content'][1]
                    ref_node_data = context.get(str(ref_node_id)).output_data
                    ref_node_data_json_str = json.dumps(ref_node_data, ensure_ascii=False)
                    ref_value = parse('$.' + ref_name).find(json.loads(ref_node_data_json_str))
                    parameter_dict[parameter_name] = ref_value[0].value
                elif input_definition.value_type == ValueTypeOfIODefinition.LITERAL:
                    parameter_dict[parameter_name] = input_definition.content

            prompt = safe_format_double_braces(prompt, **parameter_dict)
            chat_mdl = LLMBundle(db, user.id, LLMType.CHAT, model)
            history = [{"role": "user", "content": safe_format_double_braces(prompt, **parameter_dict)}]
            response = chat_mdl.chat(system="", history=history, gen_conf={})
            print(f"LLM 输出：{response}")

            context.set(str(self.node_id),
                        NodeIOData(output_data={self.component_parameter.output_definition['variable_name']: response}))

    def validate_inputs(self):
        pass

    def get_output_schema(self):
        pass

    @staticmethod
    def decode(json: json) -> 'LLMComponent':
        node_json = json['node']
        node_id = node_json['id']
        component_param = node_json['data']['componentParam']
        is_batch = component_param.get('isBatch', False)
        batch = Batch.parse_batch(component_param.get('batch', None))
        input_definition = component_param['input_definition']
        output_definition = component_param['output_definition']
        model = component_param['model']
        prompt = component_param['prompt']

        llm_component_param = LLMComponentParam(model=model, prompt=prompt, output_definition=output_definition,
                                                input_definition_list=input_definition, is_batch=is_batch, batch=batch)
        return LLMComponent(llm_component_param, node_id)

    def combine_dict_arrays(self, input_dict):
        # 获取所有数组的长度
        lengths = [len(arr) for arr in input_dict.values() if isinstance(arr, list)]

        # 如果没有数组或长度不一致，返回空列表
        if not lengths or len(set(lengths)) != 1:
            return []

        # 数组的长度
        length = lengths[0]

        # 创建结果列表
        result = []

        # 遍历数组长度次数
        for i in range(length):
            # 为每个索引创建一个新的字典
            item = {}
            for key, value in input_dict.items():
                # 只处理列表类型的值
                if isinstance(value, list):
                    item[key] = value[i] if i < len(value) else None
            result.append(item)

        return result
