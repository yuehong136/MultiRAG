import os
from dataclasses import dataclass
from typing import Union, Optional
import json
from workflow.WorkflowContext import WorkflowContext, NodeIOData
from workflow.basic.Component import Component, ComponentParameter
from workflow.basic.Node import ValueTypeOfIODefinition
from workflow.llm.VolcengineLLM import VolcengineLLM
from workflow.utils import safe_format_double_braces
from jsonpath_ng import jsonpath, parse

@dataclass
class LLMComponentInputDefinition:
    parameter_name: str
    value_type: ValueTypeOfIODefinition
    content: Union[list[str], str]


@dataclass
class LLMComponentOutputDefinition:
    variable_name: str
    variable_type: Optional[str] = None
    description: Optional[str] = None


@dataclass
class LLMComponentParam(ComponentParameter):
    model: str
    prompt: str
    output_definition: Optional[LLMComponentOutputDefinition] = None
    input_definition_list: Optional[list[LLMComponentInputDefinition]] = None


class LLMComponent(Component[LLMComponentParam]):
    def __init__(self, component_parameter: LLMComponentParam, node_id: str):
        self.name = "LLMComponent"
        self.node_id = node_id
        self.component_parameter = component_parameter
        super().__init__(component_parameter, node_id)

    async def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> dict:
        model = self.component_parameter.model
        prompt = self.component_parameter.prompt
        parameter_dict = {}
        for input_definition in self.node_parameter.input_definition_list:
            parameter_name = input_definition['parameter_name']
            if input_definition['value_type'] == ValueTypeOfIODefinition.REF.value:
                ref_node_id = input_definition['content'][0]
                ref_name = input_definition['content'][1]
                ref_node_data = context.get(ref_node_id).output_data
                ref_node_data_json_str = json.dumps(ref_node_data, ensure_ascii=False)
                ref_value = parse('$.'+ref_name).find(json.loads(ref_node_data_json_str))
                parameter_dict[parameter_name] = ref_value[0].value
            elif input_definition.value_type == ValueTypeOfIODefinition.LITERAL:
                parameter_dict[parameter_name] = input_definition.content

        prompt = safe_format_double_braces(prompt, **parameter_dict)

        api_key = os.getenv("API_KEY")
        volcengine_llm = VolcengineLLM(api_key, model=model,
                                       base_url="https://ark.cn-beijing.volces.com/api/v3")
        response = volcengine_llm.generate(prompt)
        print(f"LLM 输出：{response}")

        context.set(self.node_id,
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

        input_definition = component_param['input_definition']
        output_definition = component_param['output_definition']
        model = component_param['model']
        prompt = component_param['prompt']

        llm_component_param = LLMComponentParam(model=model, prompt=prompt, output_definition=output_definition,
                                                input_definition_list=input_definition)
        return LLMComponent(llm_component_param, node_id)
