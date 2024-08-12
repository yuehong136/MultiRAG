from dataclasses import dataclass
from typing import Optional

from workflow.WorkflowContext import WorkflowContext, NodeIOData
from workflow.basic.Component import Component, ComponentParameter


@dataclass
class FileSelectionComponentOutputDefinition:
    variable_name: str


@dataclass
class FileSelectionComponentParam(ComponentParameter):
    output_definition: FileSelectionComponentOutputDefinition


class FileSelectionComponent(Component[FileSelectionComponentParam]):
    def __init__(self, component_parameter: FileSelectionComponentParam, node_id: str):
        self.node_id = node_id
        self.component_parameter: FileSelectionComponentParam = component_parameter
        super().__init__(component_parameter, node_id)

    def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> dict:
        output_variable_name = self.component_parameter.output_definition.variable_name
        context.set(self.node_id, NodeIOData(output_data={
            output_variable_name: "/Users/naimehao/Desktop/军事机密.txt"}))

    def validate_inputs(self):
        pass

    def get_output_schema(self):
        pass
