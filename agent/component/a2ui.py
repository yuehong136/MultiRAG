#
#  Copyright 2026 The MultiRAG Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#
from typing import Any

from agent.a2ui import build_a2ui_event, parse_a2ui_commands
from agent.component.base import ComponentBase, ComponentParamBase


class A2UIParam(ComponentParamBase):
    """
    Define the A2UI component parameters.
    """

    def __init__(self):
        super().__init__()
        self.commands = []
        self.outputs = {
            "commands": {"type": "Array<Object>"},
            "surface_ids": {"type": "Array<String>"},
            "surface_id": {"type": "str"},
        }

    def check(self):
        self.check_empty(self.commands, "[A2UI] Commands")
        return True


class A2UI(ComponentBase):
    component_name = "A2UI"

    def get_input_elements(self) -> dict[str, Any]:
        return self.get_input_elements_from_text("\n".join(self._param.commands))

    def _invoke(self, **kwargs):
        commands: list[dict[str, Any]] = []
        for raw_block in self._param.commands:
            rendered = self._canvas.get_value_with_variable(raw_block or "")
            commands.extend(parse_a2ui_commands(rendered))

        event = build_a2ui_event(commands)
        self.set_output("commands", event["commands"])
        self.set_output("surface_ids", event["surface_ids"])
        self.set_output("surface_id", event["surface_id"])

    def thoughts(self) -> str:
        return ""
