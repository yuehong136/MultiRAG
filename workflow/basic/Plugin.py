from abc import ABC
from typing import TypeVar

from workflow.basic.Node import Node, NodeParameter


class PluginParameter(NodeParameter):
    pass


P = TypeVar('P', bound=PluginParameter)


class Plugin(Node[P], ABC):
    def __init__(self, plugin_parameter: P, node_id: str):
        self.node_id = node_id
        self.plugin_parameter: P = plugin_parameter
        super().__init__(plugin_parameter, node_id)
