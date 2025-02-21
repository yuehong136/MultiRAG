from .base import Component
from ..constants import ComponentType


class RadioComponent(Component):
    """描述文本组件"""
    _json_str = """
    {
  "val": "单选组件",
  "component": "radio",
  "added": true,
  "value": "",
  "id": "RADIO_M7CNKLFL",
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
    "title": "是否存在xxx行为",
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
    "fields": [
      {
        "title": "是",
        "index": 0,
        "options": [],
        "quota": "",
        "balance": ""
      },
      {
        "title": "否",
        "index": 1,
        "options": [],
        "quota": "",
        "balance": ""
      }
    ],
    "notEdit": false,
    "optionConnect": true,
    "contextVal": "已空",
    "resetCircle": "noset",
    "isRenew": false,
    "showContext": true,
    "isHide": false,
    "singleShow": false,
    "sqlValue": ""
  }
}
    """

    def __init__(self):
        super().__init__(ComponentType.INPUT)

    def set_title(self, content: str):
        self.json_data["edit"]["title"] = content
