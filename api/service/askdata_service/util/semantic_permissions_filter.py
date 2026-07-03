from typing import Any

from api.service.askdata_service.util.askdata_logger import get_askdata_logger

logger = get_askdata_logger()


def _extract_allowed_semantic_fields(user_semantic_permissions: dict[str, Any], semantic_type: str) -> tuple[list[str], list[dict[str, str]]]:
    """
    提取用户权限中允许的语义字段ID

    Args:
        user_semantic_permissions (dict): 用户语义权限配置字典
        semantic_type (str): 语义类型，'dim' 或 'metric'

    Returns:
        tuple: (allowed_ids, allowed_info)
            - allowed_ids (list): 允许的字段ID列表
            - allowed_info (list): 允许字段的详细信息列表
    """
    allowed_ids = []
    allowed_info = []

    data_permissions = user_semantic_permissions.get("dataPermissions", {})
    models = data_permissions.get("models", [])

    for model in models:
        model_id = model.get("modelId", "unknown")
        allowed_columns = model.get("allowedColumns", [])

        for column in allowed_columns:
            if column.get("semanticType") == semantic_type:
                field_id = column.get("semanticId")
                field_name = column.get("semanticName", "unknown")

                if field_id and field_id not in allowed_ids:
                    allowed_ids.append(field_id)
                    allowed_info.append({"modelId": model_id, "semanticId": field_id, "semanticName": field_name, "semanticType": semantic_type})

    return allowed_ids, allowed_info


def filter_dimensions_by_permissions(involved_dimension_id_list: list[str], user_semantic_permissions: dict[str, Any]) -> tuple[list[str], list[str]]:
    """
    根据用户权限过滤维度，返回允许和禁止的维度ID列表

    Args:
        involved_dimension_id_list (list): 包含所有维度ID的列表
        user_semantic_permissions (dict): 用户语义权限配置字典

    Returns:
        tuple: (allowed_dimensions, prohibited_dimensions)
            - allowed_dimensions (list): 允许使用的维度ID列表
            - prohibited_dimensions (list): 被禁止的维度ID列表
    """
    # 提取允许的维度ID
    allowed_dim_ids, allowed_dim_info = _extract_allowed_semantic_fields(user_semantic_permissions, "dim")

    # 分类维度
    original_count = len(involved_dimension_id_list)
    allowed_dimensions = [dim_id for dim_id in involved_dimension_id_list if dim_id in allowed_dim_ids]
    prohibited_dimensions = [dim_id for dim_id in involved_dimension_id_list if dim_id not in allowed_dim_ids]

    allowed_count = len(allowed_dimensions)
    prohibited_count = len(prohibited_dimensions)

    # 输出日志
    logger.info(f"开始筛选维度，原始维度总数: {original_count}")
    logger.info(f"权限中允许的维度数量: {len(allowed_dim_ids)}")

    if allowed_dim_info:
        for info in allowed_dim_info:
            logger.info(f"允许维度 - 模型ID: {info['modelId']}, 维度ID: {info['semanticId']}, 维度名称: {info['semanticName']}")

    if prohibited_dimensions:
        logger.info(f"被禁止的维度: {prohibited_dimensions}")

    logger.info(f"维度筛选完成，允许维度数量: {allowed_count}, 禁止维度数量: {prohibited_count}")

    return allowed_dimensions, prohibited_dimensions


def filter_metrics_by_permissions(involved_metric_id_list: list[str], user_semantic_permissions: dict[str, Any]) -> tuple[list[str], list[str]]:
    """
    根据用户权限过滤度量，返回允许和禁止的度量ID列表

    Args:
        involved_metric_id_list (list): 包含所有度量ID的列表
        user_semantic_permissions (dict): 用户语义权限配置字典

    Returns:
        tuple: (allowed_metrics, prohibited_metrics)
            - allowed_metrics (list): 允许使用的度量ID列表
            - prohibited_metrics (list): 被禁止的度量ID列表
    """
    # 提取允许的度量ID
    allowed_metric_ids, allowed_metric_info = _extract_allowed_semantic_fields(user_semantic_permissions, "metric")

    # 分类度量
    original_count = len(involved_metric_id_list)
    allowed_metrics = [metric_id for metric_id in involved_metric_id_list if metric_id in allowed_metric_ids]
    prohibited_metrics = [metric_id for metric_id in involved_metric_id_list if metric_id not in allowed_metric_ids]

    allowed_count = len(allowed_metrics)
    prohibited_count = len(prohibited_metrics)

    # 输出日志
    logger.info(f"开始筛选度量，原始度量总数: {original_count}")
    logger.info(f"权限中允许的度量数量: {len(allowed_metric_ids)}")

    if allowed_metric_info:
        for info in allowed_metric_info:
            logger.info(f"允许度量 - 模型ID: {info['modelId']}, 度量ID: {info['semanticId']}, 度量名称: {info['semanticName']}")

    if prohibited_metrics:
        logger.info(f"被禁止的度量: {prohibited_metrics}")

    logger.info(f"度量筛选完成，允许度量数量: {allowed_count}, 禁止度量数量: {prohibited_count}")

    return allowed_metrics, prohibited_metrics


def filter_semantic_fields_by_permissions(
    involved_dimension_id_list: list[str], involved_metric_id_list: list[str], user_semantic_permissions: dict[str, Any]
) -> dict[str, tuple[list[str], list[str]]]:
    """
    同时过滤维度和度量，返回完整的权限过滤结果

    Args:
        involved_dimension_id_list (list): 包含所有维度ID的列表
        involved_metric_id_list (list): 包含所有度量ID的列表
        user_semantic_permissions (dict): 用户语义权限配置字典

    Returns:
        dict: 包含维度和度量过滤结果的字典
            {
                'dimensions': (allowed_dimensions, prohibited_dimensions),
                'metrics': (allowed_metrics, prohibited_metrics)
            }
    """
    logger.info("开始同时筛选维度和度量")

    # 过滤维度
    allowed_dimensions, prohibited_dimensions = filter_dimensions_by_permissions(involved_dimension_id_list, user_semantic_permissions)

    # 过滤度量
    allowed_metrics, prohibited_metrics = filter_metrics_by_permissions(involved_metric_id_list, user_semantic_permissions)

    result = {"dimensions": (allowed_dimensions, prohibited_dimensions), "metrics": (allowed_metrics, prohibited_metrics)}

    logger.info("维度和度量筛选全部完成")

    return result


# 测试用例
if __name__ == "__main__":
    # 配置日志
    logger.basicConfig(level=logger.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    # 测试数据
    involved_dimension_id_list = ["36093203355146240", "36072606466712576", "36072606137197568", "37543487938742272", "36072606220559360"]
    involved_metric_id_list = ["37090785924167680", "37090785924167681", "37090785924167682", "37090785924167683"]

    user_semantic_permissions = {
        "userId": "wang_jiaoshou_007",
        "dataPermissions": {
            "models": [
                {
                    "modelId": "model_teachers_info_v1",
                    "allowedColumns": [
                        {"semanticType": "dim", "semanticId": "36093203355146240", "semanticName": "教师姓名"},
                        {"semanticType": "dim", "semanticId": "36072606466712576", "semanticName": "部门名称"},
                        {"semanticType": "dim", "semanticId": "36072606137197568", "semanticName": "职位"},
                        {"semanticType": "metric", "semanticId": "37090785924167680", "semanticName": "教师人数"},
                        {"semanticType": "metric", "semanticId": "37090785924167681", "semanticName": "平均年龄"},
                        # 注意：某些维度和度量不在允许列表中
                    ],
                    "rowFilter": {"logicalOperator": "OR", "rules": [{"permissionId": "perm_001", "permissionName": "计算机学院权限", "expression": "where department_name = '计算机科学与技术学院'"}]},
                }
            ]
        },
    }

    print("=" * 60)
    print("测试单独过滤维度")
    print("=" * 60)
    allowed_dims, prohibited_dims = filter_dimensions_by_permissions(involved_dimension_id_list, user_semantic_permissions)

    print(f"原始维度: {involved_dimension_id_list}")
    print(f"允许的维度: {allowed_dims}")
    print(f"被禁止的维度: {prohibited_dims}")

    print("\n" + "=" * 60)
    print("测试单独过滤度量")
    print("=" * 60)
    allowed_metrics, prohibited_metrics = filter_metrics_by_permissions(involved_metric_id_list, user_semantic_permissions)

    print(f"原始度量: {involved_metric_id_list}")
    print(f"允许的度量: {allowed_metrics}")
    print(f"被禁止的度量: {prohibited_metrics}")

    print("\n" + "=" * 60)
    print("测试同时过滤维度和度量")
    print("=" * 60)
    result = filter_semantic_fields_by_permissions(involved_dimension_id_list, involved_metric_id_list, user_semantic_permissions)

    print(f"维度过滤结果: 允许{len(result['dimensions'][0])}个，禁止{len(result['dimensions'][1])}个")
    print(f"度量过滤结果: 允许{len(result['metrics'][0])}个，禁止{len(result['metrics'][1])}个")

    print("\n" + "=" * 60)
    print("验证结果")
    print("=" * 60)

    # 验证维度
    expected_allowed_dims = ["36093203355146240", "36072606466712576", "36072606137197568"]
    expected_prohibited_dims = ["37543487938742272", "36072606220559360"]

    print("维度验证:")
    for dim_id in expected_allowed_dims:
        if dim_id in allowed_dims:
            print(f"✓ 正确: 维度 {dim_id} 在允许列表中")
        else:
            print(f"✗ 错误: 维度 {dim_id} 应该在允许列表中")

    for dim_id in expected_prohibited_dims:
        if dim_id in prohibited_dims:
            print(f"✓ 正确: 维度 {dim_id} 在禁止列表中")
        else:
            print(f"✗ 错误: 维度 {dim_id} 应该在禁止列表中")

    # 验证度量
    expected_allowed_metrics = ["37090785924167680", "37090785924167681"]
    expected_prohibited_metrics = ["37090785924167682", "37090785924167683"]

    print("\n度量验证:")
    for metric_id in expected_allowed_metrics:
        if metric_id in allowed_metrics:
            print(f"✓ 正确: 度量 {metric_id} 在允许列表中")
        else:
            print(f"✗ 错误: 度量 {metric_id} 应该在允许列表中")

    for metric_id in expected_prohibited_metrics:
        if metric_id in prohibited_metrics:
            print(f"✓ 正确: 度量 {metric_id} 在禁止列表中")
        else:
            print(f"✗ 错误: 度量 {metric_id} 应该在禁止列表中")
