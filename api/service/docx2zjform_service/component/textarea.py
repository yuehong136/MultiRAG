from .base import Component
from ..constants import ComponentType


class TextareaComponent(Component):
    """多行文本组件"""
    _json_str = """
    {
  "val": "多行文本",
  "component": "textArea",
  "added": true,
  "value": "",
  "id": "TEXTAREA_M78P9631",
  "edit": {
    "autoFillSwicth": false,
    "autoFill": "",
    "autoFillName": "",
    "historyFill": false,
    "color": "#3E3E3E",
    "required": true,
    "mindInfo": "",
    "isSusPrompt": false,
    "show": true,
    "isOptions": true,
    "isExceed": false,
    "exceedInfo": "该选项超出最多勾选数量",
    "title": "多行文本",
    "groupId": "",
    "associateForm": "",
    "associateField": "",
    "associateFields": [],
    "associateUserId": false,
    "ctnDftValue": "",
    "multiFormNotEdit": false,
    "tips": "",
    "sceneFill": false,
    "isComHide": false,
    "isBold": false,
    "requiredType": "sign",
    "comWidth": "width100",
    "minLength": 0,
    "maxLength": 1000,
    "checkContent": ""
  }
}
    """

    def __init__(self):
        super().__init__(ComponentType.INPUT)

    def set_title(self, content: str):
        self.json_data["edit"]["title"] = content
