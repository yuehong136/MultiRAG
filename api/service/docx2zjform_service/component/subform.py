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
  "id": "SUBFORM_M78O24VN",
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
        "title": "单行文本A",
        "index": 1,
        "key": 1,
        "requiredCol": false,
        "requiredType": "sign",
        "dataIndex": "option1",
        "width": 200
      },
      {
        "dataIndex": "option2",
        "key": 2,
        "index": 2,
        "title": "单行文本B",
        "requiredCol": false,
        "requiredType": "sign"
      }
    ],
    "dataSource": [
      {
        "index": "9nsfjciq6sw00000000",
        "wid": "tmp9nsfjciq6sw00000000",
        "key": "9nsfjciq6sw00000000",
        "option1": {
          "val": "单行文本A",
          "component": "input",
          "added": true,
          "value": "",
          "id": "INPUT_M78O24VO",
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
            "title": "单行文本A",
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
          "val": "单行文本B",
          "component": "input",
          "added": true,
          "value": "",
          "id": "INPUT_M78O24VS",
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
            "title": "单行文本B",
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
    "displayStyle": {
      "pc": {
        "isFreeze": false,
        "location": "before",
        "column": 1
      },
      "mobile": {
        "tilingSystem": "lengthways"
      }
    },
    "dataTemplate": {
      "index": "9nsfjciq6sw00000000",
      "wid": "tmp9nsfjciq6sw00000000",
      "key": "9nsfjciq6sw00000000",
      "option1": {
        "val": "单行文本A",
        "component": "input",
        "added": true,
        "value": "",
        "id": "INPUT_M78O24VO",
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
          "title": "单行文本A",
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
        "val": "单行文本B",
        "component": "input",
        "added": true,
        "value": "",
        "id": "INPUT_M78O24VS",
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
          "title": "单行文本B",
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
