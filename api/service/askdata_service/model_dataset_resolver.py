import logging
from typing import Dict, List, Set, Tuple
from collections import Counter

logger = logging.getLogger(__name__)


class ModelDatasetResolver:
    """
    模型和数据集解析器，负责处理模型详情构建和数据集确定逻辑
    """

    def __init__(self, semantic_api_client):
        self.semantic_api_client = semantic_api_client

    async def get_model_details_and_determine_dataset(
            self,
            model_ids: List[str],
            used_models: List[str],
            dataset_id_list: List[str]
    ) -> Tuple[Dict, Dict, List, Set]:
        """
        构建模型详情字典，并确定使用的数据集

        Args:
            model_ids: 分词检索后可能涉及的模型ID列表
            used_models: SQL中实际使用的模型名称列表
            dataset_id_list: 传入的数据集ID列表

        Returns:
            used_model_detail_dict: 使用的模型详情字典 {模型名: 模型详情}
            used_table_detail_dict: 使用的表详情字典 {表名: 模型详情}
            model_list: 真正使用的模型详情列表
            intersection_dataset_ids: 最终确定的数据集ID集合
        """
        # 1. 如果只传入一个数据集ID，直接使用该数据集，无需验证
        if len(dataset_id_list) == 1:
            logger.info(f"只传入一个数据集ID: {dataset_id_list[0]}，跳过数据集验证")

            used_model_detail_dict, used_table_detail_dict, model_list = await self._build_model_dicts(
                model_ids, used_models
            )

            return used_model_detail_dict, used_table_detail_dict, model_list, set(dataset_id_list)

        # 2. 多个数据集或无数据集的情况，需要计算模型所属的数据集
        used_model_detail_dict = {}
        used_table_detail_dict = {}
        model_list = []
        model_in_dataset_dict: Dict[str, List[str]] = {}

        # 获取模型详情
        model_detail_list = await self.semantic_api_client.get_model_detail_async(model_ids=model_ids)
        logger.info(f"获取到 {len(model_detail_list)} 个模型详情")

        # 处理实际使用的模型
        for model_detail in model_detail_list:
            if model_detail.get('modelName') in used_models:
                # 获取模型的指标和维度信息
                model_detail[
                    'dimsAndMetrics'] = await self.semantic_api_client.get_model_inds_and_dims_by_model_id_async(
                    model_id=model_detail["modelId"]
                )

                model_list.append(model_detail)
                used_model_detail_dict[model_detail["modelName"]] = model_detail
                used_table_detail_dict[model_detail["tableName"]] = model_detail

                # 收集该模型所在的数据集
                used_in_dataset_ids = [
                    dataset["datasetId"]
                    for dataset in model_detail.get("usedInDatasets", [])
                ]
                model_in_dataset_dict[model_detail["modelId"]] = used_in_dataset_ids

        logger.info(f"实际使用的模型及其数据集: {model_in_dataset_dict}")

        # 3. 确定最终使用的数据集
        final_dataset_ids = self._determine_dataset(model_in_dataset_dict, dataset_id_list)

        return used_model_detail_dict, used_table_detail_dict, model_list, final_dataset_ids

    async def _build_model_dicts(
            self,
            model_ids: List[str],
            used_models: List[str]
    ) -> Tuple[Dict, Dict, List]:
        """
        构建模型相关的字典（仅用于单数据集场景）
        """
        used_model_detail_dict = {}
        used_table_detail_dict = {}
        model_list = []

        model_detail_list = await self.semantic_api_client.get_model_detail_async(model_ids=model_ids)

        for model_detail in model_detail_list:
            if model_detail.get('modelName') in used_models:
                model_detail[
                    'dimsAndMetrics'] = await self.semantic_api_client.get_model_inds_and_dims_by_model_id_async(
                    model_id=model_detail["modelId"]
                )
                model_list.append(model_detail)
                used_model_detail_dict[model_detail["modelName"]] = model_detail
                used_table_detail_dict[model_detail["tableName"]] = model_detail

        return used_model_detail_dict, used_table_detail_dict, model_list

    def _determine_dataset(
            self,
            model_in_dataset_dict: Dict[str, List[str]],
            dataset_id_list: List[str]
    ) -> Set[str]:
        """
        确定最终使用的数据集ID
        """
        if not model_in_dataset_dict:
            logger.error("没有找到任何使用的模型")
            raise Exception("没有找到任何使用的模型，无法确定数据集")

        # 获取数据集（交集或最频繁的）
        result_dataset_ids = self._get_dataset_intersection_or_most_frequent(model_in_dataset_dict)

        # 没有找到任何数据集
        if not result_dataset_ids:
            logger.error(f"模型中没有找到任何数据集。model_in_dataset_dict: {model_in_dataset_dict}")
            raise Exception("模型中没有使用任何数据集，无法生成正确的SQL")

        # 找到多个数据集
        if len(result_dataset_ids) > 1:
            logger.warning(f"发现多个可能的数据集: {result_dataset_ids}")

            # 优先使用传入列表中的数据集
            if dataset_id_list:
                common_datasets = result_dataset_ids.intersection(set(dataset_id_list))
                if common_datasets:
                    selected = list(common_datasets)[0]
                    logger.warning(f"从 {len(common_datasets)} 个匹配的数据集中选择: {selected}")
                    return {selected}

            # 使用第一个
            selected = list(result_dataset_ids)[0]
            logger.warning(f"从 {len(result_dataset_ids)} 个数据集中选择第一个: {selected}")
            return {selected}

        # 理想情况：只有一个数据集
        logger.info(f"成功确定唯一数据集: {result_dataset_ids}")
        return result_dataset_ids

    def _get_dataset_intersection_or_most_frequent(
            self,
            data_dict: Dict[str, List[str]]
    ) -> Set[str]:
        """
        获取所有模型数据集的交集，如果没有交集则返回出现次数最多的数据集

        Args:
            data_dict: {模型ID: [数据集ID列表]}

        Returns:
            数据集ID的集合
        """
        if not data_dict:
            return set()

        # 过滤掉空列表
        non_empty_lists = [lst for lst in data_dict.values() if lst]

        if not non_empty_lists:
            return set()

        # 尝试获取交集
        result = set(non_empty_lists[0])
        for lst in non_empty_lists[1:]:
            result = result.intersection(set(lst))

        # 如果有交集，返回交集
        if result:
            logger.info(f"找到数据集交集: {result}")
            return result

        # 没有交集，统计出现频率
        logger.warning("模型之间没有共同数据集，将使用出现频率最高的数据集")

        all_datasets = []
        for lst in non_empty_lists:
            all_datasets.extend(lst)

        if not all_datasets:
            return set()

        # 统计每个数据集出现的次数
        counter = Counter(all_datasets)
        max_count = counter.most_common(1)[0][1]

        # 获取所有出现次数最多的数据集
        most_frequent_datasets = {
            dataset for dataset, count in counter.items()
            if count == max_count
        }

        logger.info(f"出现频率最高的数据集(出现{max_count}次): {most_frequent_datasets}")

        return most_frequent_datasets