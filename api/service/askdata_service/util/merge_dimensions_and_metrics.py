def merge_dimensions_and_metrics(dimension_values, dimensions, all_metrics):
    """
    合并dimension_values到dimensions中，并清理数据结构

    Args:
        dimension_values: 包含维度值的字典，key为dimensionId
        dimensions: 维度列表
        all_metrics: 指标列表

    Returns:
        list: 合并后的维度和指标列表
    """
    # 处理dimensions
    processed_dimensions = []

    for dim in dimensions:
        # 只保留指定的键
        cleaned_dim = {
            'dimensionId': dim.get('dimensionId'),
            'dimensionName': dim.get('dimensionName'),
            'dimname_en': dim.get('dimensionEnName'),
            'description': dim.get('description'),
            'modelName': dim.get('modelName'),
            'modelId': dim.get('modelId'),
            'dataobject': dim.get('dataobject')
        }

        # 合并dimension_values，最多取5个
        dimension_id = dim.get('dimensionId')
        if dimension_id and dimension_id in dimension_values:
            values = dimension_values[dimension_id]
            # 最多取5个值
            sample_values = [item['value'] for item in values[:5]]
            cleaned_dim['sampleValues'] = sample_values

        processed_dimensions.append(cleaned_dim)

    # 处理all_metrics，只保留指定的键
    processed_metrics = []
    for metric in all_metrics:
        cleaned_metric = {
            'metricId': metric.get('metricId'),
            'metricName': metric.get('metricName'),
            'metricEnName': metric.get('metricEnName'),
            'description': metric.get('description'),
            'modelName': metric.get('modelName'),
            'modelId': metric.get('modelId'),
            'dataobject': metric.get('dataobject')
        }
        processed_metrics.append(cleaned_metric)

    # 将dimensions和metrics合并到同一个列表中
    result = processed_dimensions + processed_metrics

    return result


# 示例使用
if __name__ == "__main__":
    # 你的原始数据
    dimension_values = {
        "38608720871391232": [
            {"value": "0020027c096661cb2925675ef7b9efe1"},
            {"value": "0036127c2fec37e193721f8de0934895"},
            {"value": "004cf5f234a834d06ea8f3e790ddd52b"},
            {"value": "00979866645a769ac636937f78b9e13d"},
            {"value": "00acf181f5a71f052e848405ebccd657"},
            {"value": "00d1a8b1ae2e1337c25df0d4d9228eca"},
            {"value": "00fb6da003cebecba1545420d6d665cb"},
            {"value": "010051305340f12d3d51f7604e6d58b2"},
            {"value": "0109a23a6a0c1e8f8115cf4ea4aed00b"},
            {"value": "011c28c7d9bd8a4af81d7d5514b2c79f"}
        ],
        "38608598041722880": [
            {"value": "001a82db1d1f790c6c760ec9d5a42cd0"},
            {"value": "0020027c096661cb2925675ef7b9efe1"},
            {"value": "0036127c2fec37e193721f8de0934895"},
            {"value": "0049591e79413f8ffdee0380b7fe5379"},
            {"value": "004cf5f234a834d06ea8f3e790ddd52b"},
            {"value": "0051fc86fb1a6f163c9063ddf70ec3f0"},
            {"value": "0061a29fb4ebfc05de82a0aa63ef0c24"},
            {"value": "00911e94e5d4bd13b26284b5973c363e"},
            {"value": "00979866645a769ac636937f78b9e13d"},
            {"value": "00acf181f5a71f052e848405ebccd657"}
        ]
    }

    dimensions = [
        {
            "dataType": "varchar",
            "database_wid": "38497393509964800",
            "dataobject": "t_jzg_zzmml",
            "dataset_wid": "38608841318432768",
            "datasets": [{"datasetId": "38608841318432768", "datasetName": "教师"}],
            "description": "参加党派日期",
            "dimensionEnName": "cjdprq",
            "dimensionId": "38608716050825216",
            "dimensionName": "参加党派日期",
            "dimname_en": "cjdprq",
            "dimtype": "Time",
            "domainId": "38514628910788608",
            "domainName": "bl",
            "isEnum": False,
            "is_label": "0",
            "modelId": "38608715617501184",
            "modelName": "教职工政治面貌",
            "requested_dimension_id": "38608716050825216",
            "semanticsformat": "{\"type\":\"Time\",\"timeFormat\":\"yyyy-MM-dd\",\"timeGranularity\":\"\",\"isTag\":false}",
            "status": "1"
        },
        {
            "dataType": "varchar",
            "database_wid": "38497393509964800",
            "dataobject": "t_jzg_zzmml",
            "dataset_wid": "38608841318432768",
            "datasets": [{"datasetId": "38608841318432768", "datasetName": "教师"}],
            "description": "工号",
            "dimensionEnName": "gh",
            "dimensionId": "38608720871391232",
            "dimensionName": "工号",
            "dimname_en": "gh",
            "dimtype": "HC",
            "domainId": "38514628910788608",
            "domainName": "bl",
            "isEnum": False,
            "is_label": "0",
            "modelId": "38608715617501184",
            "modelName": "教职工政治面貌",
            "requested_dimension_id": "38608720871391232",
            "semanticsformat": "{\"type\":\"HC\",\"timeFormat\":\"\",\"timeGranularity\":\"\",\"isTag\":false}",
            "status": "1"
        }
    ]

    all_metrics = [
        {
            "dataType": "numeric",
            "dataformat": "{\"dataformat\":\"default\"}",
            "dataobject": "t_ky_cghj",
            "description": "获奖人数",
            "expression": "t_ky_cghj.hjrs",
            "metricEnName": "hjrs",
            "metricId": "38695555387054080",
            "metricName": "获奖人数",
            "modelId": "38649313843940352",
            "modelName": "科研成果获奖信息"
        }
    ]

    # 调用方法
    result = merge_dimensions_and_metrics(dimension_values, dimensions, all_metrics)

    # 打印结果
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))
