import json
from dataclasses import dataclass
from typing import Optional, Union

from workflow.WorkflowContext import WorkflowContext, NodeIOData
from workflow.basic.Component import Component, ComponentParameter


@dataclass
class UserFileSelectionComponentInputDefinition:
    variable_name: str = "FILE"
    variable_type: str = "File"


@dataclass
class UserFileSelectionComponentParam(ComponentParameter):
    input_definition: UserFileSelectionComponentInputDefinition


class UserFileSelectionComponent(Component[UserFileSelectionComponentParam]):
    def __init__(self, component_parameter: UserFileSelectionComponentParam, node_id: str):
        self.name = "UserFileSelectionComponent"
        self.node_id = node_id
        self.component_parameter: UserFileSelectionComponentParam = component_parameter
        super().__init__(component_parameter, node_id, self.name)

    def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> dict:
        file = input_data.get(self.component_parameter.input_definition.variable_name)
        context.set(self.node_id,
                    NodeIOData(output_data={self.component_parameter.input_definition.variable_name: file}))

    def validate_inputs(self):
        pass

    def get_output_schema(self):
        pass

    @staticmethod
    def decode(json: json) -> 'UserFileSelectionComponent':
        node_json = json['node']
        node_id = node_json['id']
        component_param = node_json['data']['componentParam']
        variable_name = component_param['input_definition']['variable_name']
        variable_type = component_param['input_definition']['variable_type']
        user_file_selection_param = UserFileSelectionComponentParam(
            UserFileSelectionComponentInputDefinition(variable_name, variable_type))
        return UserFileSelectionComponent(user_file_selection_param, node_id)


if __name__ == "__main__":
    json_schema = """{
        "input_definition": {
            "variable_name": "FILE",
            "variable_type": "File"
        }
    }"""
    from dacite import from_dict

    data_dict = json.loads(json_schema)
    user_file_selection_param = from_dict(data_class=UserFileSelectionComponentParam, data=data_dict)
    print(user_file_selection_param)
