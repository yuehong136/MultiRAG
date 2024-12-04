def dict_arrays_to_array_dicts(data):
    # 使用最短列表的长度
    min_length = min(len(v) for v in data.values())

    result = []

    # 只遍历到最短列表的长度
    for i in range(min_length):
        new_dict = {}
        for key, value in data.items():
            new_dict[key] = value[i]
        result.append(new_dict)

    return result


if __name__ == "__main__":
    # 测试数据
    data = {
        "a": [1, 2, 3],
        "b": ["b", "b1"]
    }

    # 转换并打印结果
    result = dict_arrays_to_array_dicts(data)
    print(result)  # [{'a': 1, 'b': 'b'}, {'a': 2, 'b': 'b1'}]
