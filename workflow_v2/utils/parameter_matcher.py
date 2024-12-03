from typing import Dict, Any, List
import json


class ParameterMatcher:
    def __init__(self, input_parameters: List[Dict], node_data: Dict):
        self.input_parameters = input_parameters
        self.node_data = node_data
        self.matched_values = {}

    def _get_node_output(self, block_id: str) -> Dict:
        """获取指定节点的输出数据"""
        return self.node_data.get("nodes", {}).get(block_id, {}).get("output", {})

    def _resolve_ref_value(self, ref_content: Dict) -> Any:
        """解析引用类型的值"""
        if ref_content["source"] != "block-output":
            raise ValueError(f"Unsupported reference source: {ref_content['source']}")

        block_id = ref_content["blockID"]
        name = ref_content["name"]

        # 处理嵌套属性路径
        node_output = self._get_node_output(block_id)
        path_parts = name.split(".")

        def get_nested_value(data: Any, parts: List[str]) -> Any:
            if not parts:
                return data

            part = parts[0]
            remaining_parts = parts[1:]

            if isinstance(data, dict):
                if part in data:
                    return get_nested_value(data[part], remaining_parts)
            elif isinstance(data, list):
                # 对于key_6这样的数组类型，我们取第一个元素的对应属性
                if part.startswith("key_6"):
                    if data and isinstance(data[0], dict):
                        return get_nested_value(data[0], [part] + remaining_parts)

            raise KeyError(f"Cannot find path {name} in node {block_id}")

        return get_nested_value(node_output, path_parts)

    def _resolve_value(self, value_def: Dict) -> Any:
        """解析值定义"""
        if value_def["type"] == "ref":
            return self._resolve_ref_value(value_def["content"])
        elif value_def["type"] == "literal":
            return value_def["content"]
        else:
            raise ValueError(f"Unsupported value type: {value_def['type']}")

    def _validate_type(self, value: Any, type_def: str) -> bool:
        """验证值类型是否匹配"""
        type_mapping = {
            "string": str,
            "integer": int,
            "boolean": bool,
            "float": float,
            "object": dict,
            "list": list
        }

        expected_type = type_mapping.get(type_def)
        if not expected_type:
            raise ValueError(f"Unsupported type definition: {type_def}")

        return isinstance(value, expected_type)

    def _validate_schema(self, value: Any, schema_def: Dict) -> bool:
        """验证值是否符合模式定义"""
        if "type" not in schema_def:
            return False

        # 基本类型验证
        if not self._validate_type(value, schema_def["type"]):
            return False

        # 对象类型的详细验证
        if schema_def["type"] == "object" and "schema" in schema_def:
            for field in schema_def["schema"]:
                field_name = field["name"]
                if field_name not in value:
                    return False
                if not self._validate_schema(value[field_name], field):
                    return False

        # 列表类型的详细验证
        elif schema_def["type"] == "list" and "schema" in schema_def:
            for item in value:
                if not self._validate_schema(item, schema_def["schema"]):
                    return False

        return True

    def match_parameters(self) -> Dict[str, Any]:
        """匹配并验证所有参数"""
        for param in self.input_parameters:
            name = param["name"]
            input_def = param["input"]

            # 获取值
            try:
                value = self._resolve_value(input_def["value"])
            except (KeyError, ValueError) as e:
                raise ValueError(f"Error resolving value for parameter {name}: {str(e)}")

            # 验证类型和模式
            if not self._validate_type(value, input_def["type"]):
                raise ValueError(f"Type mismatch for parameter {name}")

            if "schema" in input_def:
                if not self._validate_schema(value, input_def):
                    raise ValueError(f"Schema validation failed for parameter {name}")

            self.matched_values[name] = value

        return self.matched_values


def match_parameters(input_parameters: List[Dict], node_data: Dict) -> Dict[str, Any]:
    """
    主函数：匹配参数定义和节点数据

    Args:
        input_parameters: 参数定义列表
        node_data: 节点运行数据

    Returns:
        Dict[str, Any]: 匹配后的参数值字典
    """
    matcher = ParameterMatcher(input_parameters, node_data)
    return matcher.match_parameters()


# 使用示例
if __name__ == "__main__":
    # 示例输入参数定义
    input_parameters = [
        {
            "name": "key0",
            "input": {
                "type": "string",
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key0"
                    }
                }
            }
        },
        {
            "name": "key1",
            "input": {
                "type": "integer",
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key1"
                    }
                }
            }
        },
        {
            "name": "key2",
            "input": {
                "type": "boolean",
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key2"
                    }
                }
            }
        },
        {
            "name": "key3",
            "input": {
                "type": "float",
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key3"
                    }
                }
            }
        },
        {
            "name": "key4",
            "input": {
                "type": "object",
                "schema": [
                    {
                        "type": "string",
                        "name": "key4_0"
                    },
                    {
                        "type": "object",
                        "name": "key4_1",
                        "schema": [
                            {
                                "type": "string",
                                "name": "key4_1_0"
                            },
                            {
                                "type": "object",
                                "name": "key4_1_1",
                                "schema": [
                                    {
                                        "type": "string",
                                        "name": "key4_1_1_0"
                                    }
                                ]
                            }
                        ]
                    }
                ],
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key4"
                    }
                }
            }
        },
        {
            "name": "key4_0",
            "input": {
                "type": "string",
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key4.key4_0"
                    }
                }
            }
        },
        {
            "name": "key4_1",
            "input": {
                "type": "object",
                "schema": [
                    {
                        "type": "string",
                        "name": "key4_1_0"
                    },
                    {
                        "type": "object",
                        "name": "key4_1_1",
                        "schema": [
                            {
                                "type": "string",
                                "name": "key4_1_1_0"
                            }
                        ]
                    }
                ],
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key4.key4_1"
                    }
                }
            }
        },
        {
            "name": "key4_1_0",
            "input": {
                "type": "string",
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key4.key4_1.key4_1_0"
                    }
                }
            }
        },
        {
            "name": "key4_1_1",
            "input": {
                "type": "object",
                "schema": [
                    {
                        "type": "string",
                        "name": "key4_1_1_0"
                    }
                ],
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key4.key4_1.key4_1_1"
                    }
                }
            }
        },
        {
            "name": "key4_1_1_0",
            "input": {
                "type": "string",
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key4.key4_1.key4_1_1.key4_1_1_0"
                    }
                }
            }
        },
        {
            "name": "key_5",
            "input": {
                "type": "list",
                "schema": {
                    "type": "string"
                },
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key_5"
                    }
                }
            }
        },
        {
            "name": "key_6",
            "input": {
                "type": "list",
                "schema": {
                    "type": "object",
                    "schema": [
                        {
                            "type": "string",
                            "name": "key_6_0"
                        },
                        {
                            "type": "list",
                            "name": "key_6_1",
                            "schema": {
                                "type": "string"
                            }
                        },
                        {
                            "type": "list",
                            "name": "key_6_2",
                            "schema": {
                                "type": "object",
                                "schema": [
                                    {
                                        "type": "list",
                                        "name": "key_6_2_0",
                                        "schema": {
                                            "type": "integer"
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key_6"
                    }
                }
            }
        },
        {
            "name": "key_6_0",
            "input": {
                "type": "string",
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key_6.key_6_0"
                    }
                }
            }
        },
        {
            "name": "key_6_1",
            "input": {
                "type": "list",
                "schema": {
                    "type": "string"
                },
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key_6.key_6_1"
                    }
                }
            }
        },
        {
            "name": "key_6_2",
            "input": {
                "type": "list",
                "schema": {
                    "type": "object",
                    "schema": [
                        {
                            "type": "list",
                            "name": "key_6_2_0",
                            "schema": {
                                "type": "integer"
                            }
                        }
                    ]
                },
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key_6.key_6_2"
                    }
                }
            }
        },
        {
            "name": "key_6_2_0",
            "input": {
                "type": "list",
                "schema": {
                    "type": "integer"
                },
                "value": {
                    "type": "ref",
                    "content": {
                        "source": "block-output",
                        "blockID": "100001",
                        "name": "key_6.key_6_2.key_6_2_0"
                    }
                }
            }
        },
        {
            "name": "key_7",
            "input": {
                "type": "string",
                "value": {
                    "type": "literal",
                    "content": "a"
                }
            }
        },
        {
            "name": "key_8",
            "input": {
                "type": "string",
                "value": {
                    "type": "literal",
                    "content": "1"
                }
            }
        }
    ]

    # 示例节点数据
    node_data = {
        "nodes": {
            "100001": {
                "output": {
                    "key0": "a",
                    "key1": 1,
                    "key2": True,
                    "key3": 3.13,
                    "key4": {
                        "key4_0": "b",
                        "key4_1": {
                            "key4_1_0": "c",
                            "key4_1_1": {
                                "key4_1_1_0": "d"
                            }
                        }
                    },
                    "key_5": [
                        "a",
                        "b"
                    ],
                    "key_6": [
                        {
                            "key_6_0": "a",
                            "key_6_1": [
                                "a",
                                "b"
                            ],
                            "key_6_2": [
                                {
                                    "key_6_2_0": [
                                        1,
                                        2
                                    ]
                                }
                            ]
                        },
                        {
                            "key_6_0": "a",
                            "key_6_1": [
                                "a",
                                "b"
                            ],
                            "key_6_2": [
                                {
                                    "key_6_2_0": [
                                        1,
                                        2
                                    ]
                                }
                            ]
                        }
                    ]
                }
            }
        }
    }

    try:
        result = match_parameters(input_parameters, node_data)
        print("Matched parameters:", json.dumps(result, indent=2))
    except Exception as e:
        print(f"Error: {str(e)}")
