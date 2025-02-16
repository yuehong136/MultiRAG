from .base import Component
from ..constants import ComponentType
from ..utils.generate_code import generate_code
from .input import InputComponent


class SubFormComponent(Component):
    """表格组件"""
    _json_str = """
    {
  "val": "子表单",
  "component": "subForm",
  "added": true,
  "value": "",
  "id": "SUBFORM_M74GXARQ",
  "edit": {
    "autoFillSwicth": false,
    "autoFill": "",
    "autoFillName": "",
    "historyFill": false,
    "color": "#3E3E3E",
    "required": false,
    "mindInfo": "",
    "isSusPrompt": false,
    "show": true,
    "isOptions": false,
    "isExceed": false,
    "exceedInfo": "该选项超出最多勾选数量",
    "title": "子表单",
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
    "isImport": false,
    "addBtn": true,
    "isCopy": false,
    "isDelete": false,
    "columns": [
      {
        "dataIndex": "option1",
        "key": 1,
        "index": 1,
        "title": "单行文本1",
        "requiredCol": false,
        "requiredType": "sign",
        "width": 200
      },
      {
        "dataIndex": "option2",
        "key": 2,
        "index": 2,
        "title": "单行文本2",
        "requiredCol": false,
        "requiredType": "sign"
      }
    ],
    "dataSource": [
      {
        "index": "50cu1kqu5qw00000000",
        "wid": "tmp50cu1kqu5qw00000000",
        "key": "50cu1kqu5qw00000000",
        "option1": {
          "val": "单行文本",
          "component": "input",
          "added": true,
          "value": "",
          "id": "INPUT_M74GXART",
          "edit": {
            "autoFillSwicth": false,
            "autoFill": "",
            "autoFillName": "",
            "historyFill": false,
            "color": "#3E3E3E",
            "required": false,
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
        },
        "option2": {
          "val": "单行文本",
          "component": "input",
          "added": true,
          "value": "",
          "id": "INPUT_M74GXARV",
          "edit": {
            "autoFillSwicth": false,
            "autoFill": "",
            "autoFillName": "",
            "historyFill": false,
            "color": "#3E3E3E",
            "required": false,
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
      }
    ],
    "dataTemplate": {
      "index": "50cu1kqu5qw00000000",
      "wid": "tmp50cu1kqu5qw00000000",
      "key": "50cu1kqu5qw00000000",
      "option1": {
        "val": "单行文本",
        "component": "input",
        "added": true,
        "value": "",
        "id": "INPUT_M74GXART",
        "edit": {
          "autoFillSwicth": false,
          "autoFill": "",
          "autoFillName": "",
          "historyFill": false,
          "color": "#3E3E3E",
          "required": false,
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
      },
      "option2": {
        "val": "单行文本",
        "component": "input",
        "added": true,
        "value": "",
        "id": "INPUT_M74GXARV",
        "edit": {
          "autoFillSwicth": false,
          "autoFill": "",
          "autoFillName": "",
          "historyFill": false,
          "color": "#3E3E3E",
          "required": false,
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
    }
  }
}
    """

    def __init__(self):
        super().__init__(ComponentType.SUBFORM)
        # 初始化子表单组件的属性
        self.json_data["edit"]["columns"] = []
        self.json_data["edit"]["dataSource"] = []
        self.json_data["edit"]["dataTemplate"] = {}
        index = generate_code(19)
        wid = "tmp" + index
        key = index

        self.json_data["edit"]["dataSource"].append({
            "index": index,
            "wid": wid,
            "key": key
        })

        self.json_data["edit"]["dataTemplate"].update({
            "index": index,
            "wid": wid,
            "key": key
        })

    def add_input_component(self, input_component: InputComponent):
        columns = self.json_data["edit"]["columns"]
        columns_count = len(columns)
        data_index = "option" + str(columns_count + 1)
        columns.append({
            "dataIndex": data_index,
            "key": columns_count + 1,
            "index": columns_count + 1,
            "title": input_component.json_data["edit"]["title"],
            "requiredCol": False,
            "requiredType": "sign"
        })

        data_source = self.json_data["edit"]["dataSource"][0]
        data_source[data_index] = input_component.json_data

        data_template = self.json_data["edit"]["dataTemplate"]
        data_template[data_index] = input_component.json_data
