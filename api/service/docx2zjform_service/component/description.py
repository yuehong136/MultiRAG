from .base import Component
from ..constants import ComponentType


class DescriptionComponent(Component):
    """描述文本组件"""
    _json_str = """
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

    def __init__(self):
        super().__init__(ComponentType.DESCRIPTION)

    def set_content(self, content: str):
        """设置描述内容"""
        self.json_data["edit"]["editorState"] = content

    def get_content(self) -> str:
        """获取描述内容"""
        return self.json_data["edit"]["editorState"]
