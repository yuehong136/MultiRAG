from dataclasses import dataclass

from workflow.basic.Component import Component, ComponentParameter
from workflow.WorkflowContext import NodeIOData, WorkflowContext


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

    async def process(self, input_data: dict | None = None, context: WorkflowContext | None = None, **kwargs) -> dict:
        output_variable_name = self.component_parameter.output_definition.variable_name
        file = input_data.get(output_variable_name)
        context.set(str(self.node_id), NodeIOData(output_data={output_variable_name: file}))

    def validate_inputs(self):
        pass

    def get_output_schema(self):
        pass
