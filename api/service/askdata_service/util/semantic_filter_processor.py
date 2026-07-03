"""
语义过滤处理器模块
用于处理语义相关性过滤后的维度和指标
"""

from api.service.askdata_service.util.askdata_logger import get_askdata_logger

logger = get_askdata_logger()


class SemanticFilterProcessor:
    """语义过滤处理器，负责处理LLM返回的排除列表并更新维度和指标"""

    @staticmethod
    def process_excluded_fields(
            dimensions: list[dict],
            all_metrics: list[dict],
            exclude_dim_and_metric: dict[str, list[str]],
            log_details: bool = True
    ) -> tuple[list[dict], list[dict], dict[str, list[dict]]]:
        """
        处理被排除的字段，打印信息并更新列表

        参数:
            dimensions: 原始维度列表
            all_metrics: 原始指标列表
            exclude_dim_and_metric: 包含排除列表的字典 {"excludeDim": [...], "excludeMetric": [...]}
            log_details: 是否打印详细日志

        返回:
            Tuple[更新后的维度列表, 更新后的指标列表, 被排除字段的详细信息]
        """
        # 获取被排除的维度和指标ID
        excluded_dim_ids = set(exclude_dim_and_metric.get("excludeDim", []))
        excluded_metric_ids = set(exclude_dim_and_metric.get("excludeMetric", []))

        # 收集被排除字段的详细信息
        excluded_fields_detail = {
            'excluded_dimensions': [],
            'excluded_metrics': [],
            'statistics': {}
        }

        # 处理被排除的维度
        if excluded_dim_ids:
            for dim in dimensions:
                dim_id = dim.get('dimensionId')
                if dim_id in excluded_dim_ids:
                    dim_info = {
                        'id': dim_id,
                        'name': dim.get('dimensionName', 'Unknown'),
                        'enName': dim.get('dimensionEnName', ''),
                        'modelName': dim.get('modelName', ''),
                        'description': dim.get('description', '')
                    }
                    excluded_fields_detail['excluded_dimensions'].append(dim_info)

        # 处理被排除的指标
        if excluded_metric_ids:
            for metric in all_metrics:
                metric_id = metric.get('metricId')
                if metric_id in excluded_metric_ids:
                    metric_info = {
                        'id': metric_id,
                        'name': metric.get('metricName', 'Unknown'),
                        'enName': metric.get('metricEnName', ''),
                        'modelName': metric.get('modelName', ''),
                        'description': metric.get('description', '')
                    }
                    excluded_fields_detail['excluded_metrics'].append(metric_info)

        # 计算统计信息
        original_dim_count = len(dimensions)
        original_metric_count = len(all_metrics)
        excluded_dim_count = len(excluded_dim_ids)
        excluded_metric_count = len(excluded_metric_ids)

        # 更新维度和指标列表（使用列表切片保持引用）
        dimensions[:] = [
            dim for dim in dimensions
            if dim.get('dimensionId') not in excluded_dim_ids
        ]

        all_metrics[:] = [
            metric for metric in all_metrics
            if metric.get('metricId') not in excluded_metric_ids
        ]

        # 更新统计信息
        excluded_fields_detail['statistics'] = {
            'original': {
                'dimensions': original_dim_count,
                'metrics': original_metric_count,
                'total': original_dim_count + original_metric_count
            },
            'excluded': {
                'dimensions': excluded_dim_count,
                'metrics': excluded_metric_count,
                'total': excluded_dim_count + excluded_metric_count
            },
            'retained': {
                'dimensions': len(dimensions),
                'metrics': len(all_metrics),
                'total': len(dimensions) + len(all_metrics)
            },
            'exclusion_rate': {
                'dimensions': (excluded_dim_count / original_dim_count * 100) if original_dim_count > 0 else 0,
                'metrics': (excluded_metric_count / original_metric_count * 100) if original_metric_count > 0 else 0,
                'total': ((excluded_dim_count + excluded_metric_count) /
                          (original_dim_count + original_metric_count) * 100)
                if (original_dim_count + original_metric_count) > 0 else 0
            }
        }

        # 打印统计信息
        if log_details:
            logger.info("语义相关性过滤结果统计:")
            logger.info(f"  - 原始维度数量: {original_dim_count}, 排除: {excluded_dim_count}, "
                        f"保留: {len(dimensions)} (排除率: {excluded_fields_detail['statistics']['exclusion_rate']['dimensions']:.1f}%)")
            logger.info(f"  - 原始指标数量: {original_metric_count}, 排除: {excluded_metric_count}, "
                        f"保留: {len(all_metrics)} (排除率: {excluded_fields_detail['statistics']['exclusion_rate']['metrics']:.1f}%)")
            logger.info(f"  - 总计: 原始 {original_dim_count + original_metric_count} 个字段, "
                        f"排除 {excluded_dim_count + excluded_metric_count} 个字段, "
                        f"保留 {len(dimensions) + len(all_metrics)} 个字段 "
                        f"(总排除率: {excluded_fields_detail['statistics']['exclusion_rate']['total']:.1f}%)")

        return dimensions, all_metrics, excluded_fields_detail

    @staticmethod
    def get_excluded_field_names(excluded_fields_detail: dict[str, list[dict]]) -> dict[str, list[str]]:
        """
        从详细信息中提取被排除字段的名称列表

        参数:
            excluded_fields_detail: 被排除字段的详细信息

        返回:
            包含维度名称和指标名称列表的字典
        """
        return {
            'dimension_names': [dim['name'] for dim in excluded_fields_detail.get('excluded_dimensions', [])],
            'metric_names': [metric['name'] for metric in excluded_fields_detail.get('excluded_metrics', [])]
        }

    @staticmethod
    def get_excluded_field_ids(excluded_fields_detail: dict[str, list[dict]]) -> dict[str, list[str]]:
        """
        从详细信息中提取被排除字段的ID列表

        参数:
            excluded_fields_detail: 被排除字段的详细信息

        返回:
            包含维度ID和指标ID列表的字典
        """
        return {
            'dimension_ids': [dim['id'] for dim in excluded_fields_detail.get('excluded_dimensions', [])],
            'metric_ids': [metric['id'] for metric in excluded_fields_detail.get('excluded_metrics', [])]
        }

    @staticmethod
    def format_exclusion_summary(excluded_fields_detail: dict[str, list[dict]]) -> str:
        """
        格式化排除字段的摘要信息

        参数:
            excluded_fields_detail: 被排除字段的详细信息

        返回:
            格式化的摘要字符串
        """
        stats = excluded_fields_detail.get('statistics', {})
        excluded = stats.get('excluded', {})

        if excluded.get('total', 0) == 0:
            return "未排除任何字段"

        summary_lines = [
            "=== 语义过滤摘要 ===",
            f"排除维度: {excluded.get('dimensions', 0)} 个",
            f"排除指标: {excluded.get('metrics', 0)} 个",
            f"总计排除: {excluded.get('total', 0)} 个字段"
        ]

        # 添加部分被排除的字段名称（最多显示3个）
        excluded_dims = excluded_fields_detail.get('excluded_dimensions', [])[:3]
        excluded_mets = excluded_fields_detail.get('excluded_metrics', [])[:3]

        if excluded_dims:
            dim_names = ', '.join([d['name'] for d in excluded_dims])
            if len(excluded_fields_detail.get('excluded_dimensions', [])) > 3:
                dim_names += " ..."
            summary_lines.append(f"被排除维度示例: {dim_names}")

        if excluded_mets:
            met_names = ', '.join([m['name'] for m in excluded_mets])
            if len(excluded_fields_detail.get('excluded_metrics', [])) > 3:
                met_names += " ..."
            summary_lines.append(f"被排除指标示例: {met_names}")

        return "\n".join(summary_lines)


# 便捷函数，供快速调用
def apply_semantic_filter(
        dimensions: list[dict],
        all_metrics: list[dict],
        exclude_dim_and_metric: dict[str, list[str]],
        log_details: bool = True
) -> tuple[list[dict], list[dict], dict[str, list[dict]]]:
    """
    应用语义过滤的便捷函数

    参数:
        dimensions: 原始维度列表
        all_metrics: 原始指标列表
        exclude_dim_and_metric: 包含排除列表的字典
        log_details: 是否打印详细日志

    返回:
        Tuple[更新后的维度列表, 更新后的指标列表, 被排除字段的详细信息]
    """
    processor = SemanticFilterProcessor()
    return processor.process_excluded_fields(
        dimensions,
        all_metrics,
        exclude_dim_and_metric,
        log_details
    )
