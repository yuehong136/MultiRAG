import json
from typing import Dict, Any

from .base import Component
from ..constants import ComponentType


class DescriptionComponent(Component):
    """描述文本组件"""

    def __init__(self):
        super().__init__(ComponentType.DESCRIPTION)

    def _get_original_json(self) -> Dict[str, Any]:
        json_str = """
        {
  "val": "文本描述",
  "component": "description",
  "added": true,
  "value": "",
  "id": "DESCRIPTION_M718ST30",
  "edit": {
    "autoFill": "",
    "autoFillName": "",
    "historyFill": false,
    "color": "#3E3E3E",
    "required": false,
    "mindInfo": "",
    "show": true,
    "isOptions": true,
    "title": "描述",
    "editorState": "<p><strong><em>hello</em></strong></p>",
    "groupId": "",
    "isHiddenTitle": true
  }
}
        """
        data = json.loads(json_str)
        return data

    def set_content(self, content: str):
        self.json_data["editorState"] = content
