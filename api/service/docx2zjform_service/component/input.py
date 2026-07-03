from ..constants import ComponentType
from .base import Component


class InputComponent(Component):
    """描述文本组件"""
    _json_str = """
    {
  "val": "单行文本",
  "component": "input",
  "added": true,
  "value": "",
  "id": "INPUT_M718ST2Y",
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
    "title": "单行文本",
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
    "maxLength": 50,
    "notEdit": false,
    "inputType": "",
    "checkContent": "",
    "regexValue": "",
    "openRule": false,
    "checkRule": ""
  }
}
    """

    def __init__(self):
        super().__init__(ComponentType.INPUT)

    def set_title(self, content: str):
        self.json_data["edit"]["title"] = content
