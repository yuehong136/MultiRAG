import os
from dataclasses import dataclass
from typing import Union, Optional
import json
from workflow.WorkflowContext import WorkflowContext
from workflow.basic.Component import Component, ComponentParameter
from workflow.basic.Node import ValueTypeOfIODefinition, RefContentOfInputDefinition
from workflow.llm.VolcengineLLM import VolcengineLLM


@dataclass
class LLMComponentInputDefinition:
    parameter_name: str
    value_type: ValueTypeOfIODefinition
    content: Union[RefContentOfInputDefinition, str]


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

    def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> dict:
        input_data = input_data if input_data is not None else {}
        for input_definition in self.component_parameter.input_definition_list:
            if input_definition.value_type == ValueTypeOfIODefinition.REF:
                input_data[input_definition.parameter_name] = context.get(
                    input_definition.content.node_id).output_data.get(
                    input_definition.content.name)
            elif input_definition.value_type == ValueTypeOfIODefinition.LITERAL:
                input_data[input_definition.parameter_name] = input_definition.content

        prompt = self.component_parameter.prompt
        for input_definition in self.component_parameter.input_definition_list:
            variable_name = "{{" + input_definition.parameter_name + "}}"
            prompt = self.component_parameter.prompt.replace(variable_name, input_data[input_definition.parameter_name])

        api_key = os.getenv("API_KEY")
        volcengine_llm = VolcengineLLM(api_key, model="ep-20240808173556-h7vxq",
                                       base_url="https://ark.cn-beijing.volces.com/api/v3")
        response = volcengine_llm.generate(prompt)
        print(response)

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
