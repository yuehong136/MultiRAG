#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Enhanced API Client for DRM Semantic OpenAPI
用于向DRM语义化API服务发送请求的客户端（带自动分页和异步功能）
"""

import requests
import json
import logging
import asyncio
import aiohttp
from typing import List, Dict, Any, Optional, Union, Callable
from urllib.parse import urljoin

from api.settings import DCS_SERVER_PROTOCOL, DCS_SERVER_HOST, DCS_SERVER_PORT

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SemanticApiClient:
    """DRM API客户端类，用于向DRM服务发送各类API请求"""

    def __init__(
            self,
            protocol: str = None,
            host: str = None,
            port: str = None,
            token: str = None,
            timeout: int = 30
    ):
        """
        初始化DRM API客户端

        Args:
            protocol: 服务协议 (http/https)
            host: 服务器主机名或IP
            port: 服务器端口
            token: 认证令牌(可选)
            timeout: 请求超时时间(秒)
        """
        self.protocol = protocol or DCS_SERVER_PROTOCOL
        self.host = host or DCS_SERVER_HOST
        self.port = port or DCS_SERVER_PORT
        self.token = token
        self.timeout = timeout
        self.base_url = f"{self.protocol}://{self.host}:{self.port}"

        # 常用API路径
        self.api_paths = {
            "get_dimension_info": "/api/drm/semanticOpenApi/getDimensionInfoByKeyword",
            "get_dimension_by_dimension_value": "/api/drm/semanticOpenApi/getDimensionByDimensionValue",
            "get_metric_info": "/api/drm/semanticOpenApi/getMetricInfoByKeyword",
            "get_business_term_info": "/api/drm/semanticOpenApi/getBusinessTermInfo",
            "get_model_detail": "/api/drm/semanticOpenApi/getModelDetail",
            "get_dimension_info_by_id": "/api/drm/semanticOpenApi/getDimensionInfoById",
        }

        # 设置请求头
        self.headers = {
            "Content-Type": "application/json"
        }
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    def _make_request(
            self,
            method: str,
            api_path: str,
            params: Dict = None,
            data: Dict = None
    ) -> Dict:
        """
        发送请求并处理响应

        Args:
            method: 请求方法 (GET, POST, PUT, DELETE等)
            api_path: API路径
            params: URL参数
            data: 请求体数据

        Returns:
            Dict: API响应结果
        """
        url = urljoin(self.base_url, api_path)

        try:
            logger.info(f"Sending {method} request to {url}")
            if data:
                logger.debug(f"Request data: {json.dumps(data, ensure_ascii=False)}")

            response = requests.request(
                method=method,
                url=url,
                params=params,
                json=data,
                headers=self.headers,
                timeout=self.timeout
            )

            # 检查HTTP状态码
            response.raise_for_status()

            # 解析响应JSON
            result = response.json()
            logger.debug(f"Response: {json.dumps(result, ensure_ascii=False)}")

            # 检查业务状态码
            if "code" in result and str(result["code"]) != "0":
                logger.warning(f"Business error: {result.get('msg', 'Unknown error')}")

            return result

        except requests.exceptions.RequestException as e:
            logger.error(f"Request error: {str(e)}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            raise

    async def _make_async_request(
            self,
            method: str,
            api_path: str,
            params: Dict = None,
            data: Dict = None
    ) -> Dict:
        """
        异步发送请求并处理响应

        Args:
            method: 请求方法 (GET, POST, PUT, DELETE等)
            api_path: API路径
            params: URL参数
            data: 请求体数据

        Returns:
            Dict: API响应结果
        """
        url = urljoin(self.base_url, api_path)

        try:
            logger.info(f"Sending async {method} request to {url}")
            if data:
                logger.debug(f"Async request data: {json.dumps(data, ensure_ascii=False)}")

            async with aiohttp.ClientSession() as session:
                async with session.request(
                        method=method,
                        url=url,
                        params=params,
                        json=data,
                        headers=self.headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as response:
                    # 检查HTTP状态码
                    response.raise_for_status()

                    # 解析响应JSON
                    result = await response.json()
                    logger.debug(f"Async response: {json.dumps(result, ensure_ascii=False)}")

                    # 检查业务状态码
                    if "code" in result and str(result["code"]) != "0":
                        logger.warning(f"Business error: {result.get('msg', 'Unknown error')}")

                    return result

        except aiohttp.ClientError as e:
            logger.error(f"Async request error: {str(e)}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Async JSON decode error: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Unexpected async error: {str(e)}")
            raise

    async def get_dimension_info_by_keyword_async(
            self,
            keyword: Union[str, List[str]],
            dataset_ids: List[str],
            fuzzy_match: bool = True,
            page_size: int = 100,
            max_pages: int = 100,
            extract_rows: bool = True,
            max_concurrent: int = 5,
            deduplicate_by_dimension_id: bool = True
    ) -> Union[Dict, List[Dict]]:
        """
        异步获取所有维度信息（支持多关键词，自动分页，并发请求，支持维度ID去重）

        Args:
            keyword: 搜索关键词或关键词列表
            dataset_ids: 数据集ID列表
            fuzzy_match: 是否模糊匹配
            page_size: 每页大小
            max_pages: 最大页数限制，防止无限请求
            extract_rows: 是否只提取rows部分数据
            max_concurrent: 最大并发请求数
            deduplicate_by_dimension_id: 是否根据维度ID去重

        Returns:
            Union[Dict, List[Dict]]: 合并后的完整响应或只包含所有rows的列表（去重后）
        """
        logger.info(f"\n=== 根据维度名称搜索维度信息 ===")
        logger.info(f"dataset_ids: {dataset_ids}")
        # 转换单个关键词为列表
        if isinstance(keyword, str):
            keywords = [keyword]
        else:
            keywords = keyword

        all_results = []
        dimension_id_set = set()  # 用于跟踪已见过的dimensionId

        try:
            # 为每个关键词创建异步任务
            for kw in keywords:
                logger.info(f"正在处理维度信息关键词: {kw}")

                # 第一次请求，获取总数
                first_result = await self._make_async_request(
                    "POST",
                    self.api_paths["get_dimension_info"],
                    params={"pi": 1, "ps": page_size},
                    data={"keyword": kw, "datasetIds": dataset_ids, "fuzzyMatch": fuzzy_match}
                )

                if str(first_result.get("code", "")) != "0":
                    logger.warning(f"获取关键词'{kw}'的第一页失败: {first_result.get('msg', '未知错误')}")
                    continue

                # 计算总页数
                data = first_result.get("data", {})
                total = int(data.get("total", 0))
                total_pages = (total + page_size - 1) // page_size  # 向上取整

                # 获取第一页结果
                keyword_results = []
                duplicate_count = 0
                if extract_rows:
                    first_page_rows = data.get("rows", [])

                    # 处理第一页结果（如果需要去重）
                    for row in first_page_rows:
                        dimension_id = row.get('dimensionId')
                        if deduplicate_by_dimension_id:
                            if dimension_id and dimension_id in dimension_id_set:
                                duplicate_count += 1
                                continue
                            if dimension_id:
                                dimension_id_set.add(dimension_id)

                        keyword_results.append(row)

                    logger.info(
                        f"关键词'{kw}'的第一页返回了{len(first_page_rows)}条结果，去重后保留{len(first_page_rows) - duplicate_count}条")

                    # 打印第一页结果的维度ID和维度名称
                    if keyword_results:
                        for idx, row in enumerate(keyword_results[:min(3, len(keyword_results))]):  # 只打印前3条
                            dimension_id = row.get('dimensionId', 'N/A')
                            dimension_name = row.get('dimensionName', 'N/A')
                            dimension_code = row.get('dimensionCode', 'N/A')
                            logger.info(
                                f"  '{kw}'的结果{idx + 1}: 维度ID={dimension_id}, 维度名称={dimension_name}, 维度编码={dimension_code}")

                        if len(keyword_results) > 3:
                            logger.info(f"  ...以及第一页的其他{len(keyword_results) - 3}条结果")
                else:
                    keyword_results.append(first_result)
                    logger.info(f"关键词'{kw}'的第一页已返回")

                # 限制最大页数
                total_pages = min(total_pages, max_pages)

                if total_pages <= 1:
                    logger.info(f"关键词'{kw}'的总唯一结果数: {len(keyword_results)}条")
                    all_results.extend(keyword_results)
                    continue

                # 创建剩余页面的异步任务
                page_tasks = []
                for page in range(2, total_pages + 1):
                    task = self._make_async_request(
                        "POST",
                        self.api_paths["get_dimension_info"],
                        params={"pi": page, "ps": page_size},
                        data={"keyword": kw, "datasetIds": dataset_ids, "fuzzyMatch": fuzzy_match}
                    )
                    page_tasks.append(task)

                # 限制并发请求数
                page_results = []
                for i in range(0, len(page_tasks), max_concurrent):
                    batch = page_tasks[i:i + max_concurrent]
                    batch_results = await asyncio.gather(*batch, return_exceptions=True)
                    page_results.extend(batch_results)

                # 处理结果
                additional_total_rows = 0
                additional_kept_rows = 0
                additional_results = []
                for result in page_results:
                    if isinstance(result, Exception):
                        logger.error(f"关键词'{kw}'的异步请求出错: {str(result)}")
                        continue

                    if str(result.get("code", "")) == "0":
                        if extract_rows:
                            rows = result.get("data", {}).get("rows", [])
                            additional_total_rows += len(rows)

                            # 处理额外页面结果（如果需要去重）
                            for row in rows:
                                dimension_id = row.get('dimensionId')
                                if deduplicate_by_dimension_id:
                                    if dimension_id and dimension_id in dimension_id_set:
                                        continue
                                    if dimension_id:
                                        dimension_id_set.add(dimension_id)

                                keyword_results.append(row)
                                additional_results.append(row)
                                additional_kept_rows += 1
                        else:
                            keyword_results.append(result)
                            additional_kept_rows += 1
                            additional_total_rows += 1

                logger.info(
                    f"关键词'{kw}'的额外页面返回了{additional_total_rows}条结果，去重后保留{additional_kept_rows}条")

                # 打印额外页面中的一些结果信息
                if additional_results and extract_rows:
                    sample_size = min(3, len(additional_results))
                    logger.info(f"  关键词'{kw}'的额外页面样本:")
                    for idx, row in enumerate(additional_results[:sample_size]):
                        dimension_id = row.get('dimensionId', 'N/A')
                        dimension_name = row.get('dimensionName', 'N/A')
                        dimension_code = row.get('dimensionCode', 'N/A')
                        logger.info(
                            f"  '{kw}'的额外结果{idx + 1}: 维度ID={dimension_id}, 维度名称={dimension_name}, 维度编码={dimension_code}")

                    if len(additional_results) > 3:
                        logger.info(f"  ...以及额外页面的其他{len(additional_results) - 3}条结果")

                # 统计每个维度ID出现的次数，了解结果分布
                if extract_rows and keyword_results:
                    dimension_counts = {}
                    for row in keyword_results:
                        dim_id = row.get('dimensionId')
                        dim_name = row.get('dimensionName', 'N/A')
                        if dim_id:
                            if dim_id not in dimension_counts:
                                dimension_counts[dim_id] = {'count': 0, 'name': dim_name}
                            dimension_counts[dim_id]['count'] += 1

                    logger.info(f"  关键词'{kw}'的结果分布:")
                    for dim_id, info in dimension_counts.items():
                        logger.info(f"  - 维度ID={dim_id}, 维度名称={info['name']}: {info['count']}条结果")

                logger.info(f"关键词'{kw}'的总唯一结果数: {len(keyword_results)}条")

                # 添加到总结果
                all_results.extend(keyword_results)

            logger.info(f"所有维度信息关键词合并后的总唯一结果数: {len(all_results)}条")

            # 最后检查一遍确保没有重复的dimensionId（以防万一）
            if deduplicate_by_dimension_id and extract_rows:
                final_unique_results = []
                final_dimension_id_set = set()
                for result in all_results:
                    dimension_id = result.get('dimensionId')
                    if dimension_id and dimension_id in final_dimension_id_set:
                        continue
                    if dimension_id:
                        final_dimension_id_set.add(dimension_id)
                    final_unique_results.append(result)

                # 如果有发现额外的重复项（不应该发生，但以防万一）
                if len(final_unique_results) < len(all_results):
                    logger.warning(
                        f"最终检查时发现额外的重复项: 移除了{len(all_results) - len(final_unique_results)}条重复结果")
                    all_results = final_unique_results

            return all_results

        except Exception as e:
            logger.error(f"多关键词维度信息搜索过程中出错: {str(e)}")
            raise

    async def get_dimension_by_dimension_value_async(
            self,
            keyword: Union[str, List[str]],
            dataset_ids: List[str],
            fuzzy_match: bool = True,
            page_size: int = 100,
            max_pages: int = 100,
            extract_rows: bool = True,
            max_concurrent: int = 5,
            deduplicate_by_dimension_id: bool = True
    ) -> Union[Dict, List[Dict]]:
        """
        异步搜索维度值（支持多关键词，自动分页，并发请求，支持维度ID去重）

        Args:
            keyword: 搜索关键词或关键词列表
            dataset_ids: 数据集ID列表
            fuzzy_match: 是否模糊匹配
            page_size: 每页大小
            max_pages: 最大页数限制，防止无限请求
            extract_rows: 是否只提取rows部分数据
            max_concurrent: 最大并发请求数
            deduplicate_by_dimension_id: 是否根据维度ID去重

        Returns:
            Union[Dict, List[Dict]]: 合并后的完整响应或只包含所有rows的列表（去重后）
        """
        logger.info(f"\n=== 根据维度值名称搜索维度信息 ===")
        logger.info(f"dataset_ids: {dataset_ids}")
        # 转换单个关键词为列表
        if isinstance(keyword, str):
            keywords = [keyword]
        else:
            keywords = keyword

        all_results = []
        dimension_id_set = set()  # 用于跟踪已见过的dimensionId

        try:
            # 为每个关键词创建异步任务
            for kw in keywords:
                logger.info(f"正在处理维度值关键词: {kw}")

                # 第一次请求，获取总数
                first_result = await self._make_async_request(
                    "POST",
                    self.api_paths["get_dimension_by_dimension_value"],
                    params={"pi": 1, "ps": page_size},
                    data={"keyword": kw, "datasetIds": dataset_ids, "fuzzyMatch": fuzzy_match}
                )

                if str(first_result.get("code", "")) != "0":
                    logger.warning(f"获取关键词'{kw}'的第一页失败: {first_result.get('msg', '未知错误')}")
                    continue

                # 计算总页数
                data = first_result.get("data", {})
                total = int(data.get("total", 0))
                total_pages = (total + page_size - 1) // page_size  # 向上取整

                # 获取第一页结果
                keyword_results = []
                duplicate_count = 0
                if extract_rows:
                    first_page_rows = data.get("rows", [])

                    # 处理第一页结果（如果需要去重）
                    for row in first_page_rows:
                        dimension_id = row.get('dimensionId')
                        if deduplicate_by_dimension_id:
                            if dimension_id and dimension_id in dimension_id_set:
                                duplicate_count += 1
                                continue
                            if dimension_id:
                                dimension_id_set.add(dimension_id)

                        keyword_results.append(row)

                    logger.info(
                        f"关键词'{kw}'的第一页返回了{len(first_page_rows)}条结果，去重后保留{len(first_page_rows) - duplicate_count}条")

                    # 打印第一页结果的维度ID和维度名称
                    if keyword_results:
                        for idx, row in enumerate(keyword_results[:min(3, len(keyword_results))]):  # 只打印前3条
                            dimension_id = row.get('dimensionId', 'N/A')
                            dimension_name = row.get('dimensionName', 'N/A')
                            logger.info(f"  '{kw}'的结果{idx + 1}: 维度ID={dimension_id}, 维度名称={dimension_name}")

                        if len(keyword_results) > 3:
                            logger.info(f"  ...以及第一页的其他{len(keyword_results) - 3}条结果")
                else:
                    keyword_results.append(first_result)
                    logger.info(f"关键词'{kw}'的第一页已返回")

                # 限制最大页数
                total_pages = min(total_pages, max_pages)

                if total_pages <= 1:
                    logger.info(f"关键词'{kw}'的总唯一结果数: {len(keyword_results)}条")
                    all_results.extend(keyword_results)
                    continue

                # 创建剩余页面的异步任务
                page_tasks = []
                for page in range(2, total_pages + 1):
                    task = self._make_async_request(
                        "POST",
                        self.api_paths["get_dimension_by_dimension_value"],
                        params={"pi": page, "ps": page_size},
                        data={"keyword": kw, "datasetIds": dataset_ids, "fuzzyMatch": fuzzy_match}
                    )
                    page_tasks.append(task)

                # 限制并发请求数
                page_results = []
                for i in range(0, len(page_tasks), max_concurrent):
                    batch = page_tasks[i:i + max_concurrent]
                    batch_results = await asyncio.gather(*batch, return_exceptions=True)
                    page_results.extend(batch_results)

                # 处理结果
                additional_total_rows = 0
                additional_kept_rows = 0
                additional_results = []
                for result in page_results:
                    if isinstance(result, Exception):
                        logger.error(f"关键词'{kw}'的异步请求出错: {str(result)}")
                        continue

                    if str(result.get("code", "")) == "0":
                        if extract_rows:
                            rows = result.get("data", {}).get("rows", [])
                            additional_total_rows += len(rows)

                            # 处理额外页面结果（如果需要去重）
                            for row in rows:
                                dimension_id = row.get('dimensionId')
                                if deduplicate_by_dimension_id:
                                    if dimension_id and dimension_id in dimension_id_set:
                                        continue
                                    if dimension_id:
                                        dimension_id_set.add(dimension_id)

                                keyword_results.append(row)
                                additional_results.append(row)
                                additional_kept_rows += 1
                        else:
                            keyword_results.append(result)
                            additional_kept_rows += 1
                            additional_total_rows += 1

                logger.info(
                    f"关键词'{kw}'的额外页面返回了{additional_total_rows}条结果，去重后保留{additional_kept_rows}条")

                # 打印额外页面中的一些结果信息
                if additional_results and extract_rows:
                    sample_size = min(3, len(additional_results))
                    logger.info(f"  关键词'{kw}'的额外页面样本:")
                    for idx, row in enumerate(additional_results[:sample_size]):
                        dimension_id = row.get('dimensionId', 'N/A')
                        dimension_name = row.get('dimensionName', 'N/A')
                        logger.info(f"  '{kw}'的额外结果{idx + 1}: 维度ID={dimension_id}, 维度名称={dimension_name}")

                    if len(additional_results) > 3:
                        logger.info(f"  ...以及额外页面的其他{len(additional_results) - 3}条结果")

                # 统计每个维度ID出现的次数，了解结果分布
                if extract_rows and keyword_results:
                    dimension_counts = {}
                    for row in keyword_results:
                        dim_id = row.get('dimensionId')
                        dim_name = row.get('dimensionName', 'N/A')
                        if dim_id:
                            if dim_id not in dimension_counts:
                                dimension_counts[dim_id] = {'count': 0, 'name': dim_name}
                            dimension_counts[dim_id]['count'] += 1

                    logger.info(f"  关键词'{kw}'的结果分布:")
                    for dim_id, info in dimension_counts.items():
                        logger.info(f"  - 维度ID={dim_id}, 维度名称={info['name']}: {info['count']}条结果")

                logger.info(f"关键词'{kw}'的总唯一结果数: {len(keyword_results)}条")

                # 添加到总结果
                all_results.extend(keyword_results)

            logger.info(f"所有维度值关键词合并后的总唯一结果数: {len(all_results)}条")

            # 最后检查一遍确保没有重复的dimensionId（以防万一）
            if deduplicate_by_dimension_id and extract_rows:
                final_unique_results = []
                final_dimension_id_set = set()
                for result in all_results:
                    dimension_id = result.get('dimensionId')
                    if dimension_id and dimension_id in final_dimension_id_set:
                        continue
                    if dimension_id:
                        final_dimension_id_set.add(dimension_id)
                    final_unique_results.append(result)

                # 如果有发现额外的重复项（不应该发生，但以防万一）
                if len(final_unique_results) < len(all_results):
                    logger.warning(
                        f"最终检查时发现额外的重复项: 移除了{len(all_results) - len(final_unique_results)}条重复结果")
                    all_results = final_unique_results

            return all_results

        except Exception as e:
            logger.error(f"多关键词维度值搜索过程中出错: {str(e)}")
            raise

    async def get_metric_info_by_keyword_async(
            self,
            keyword: Union[str, List[str]],
            dataset_ids: List[str],
            fuzzy_match: bool = True,
            page_size: int = 100,
            max_pages: int = 100,
            extract_rows: bool = True,
            max_concurrent: int = 5,
            deduplicate_by_metric_id: bool = True
    ) -> Union[Dict, List[Dict]]:
        """
        异步获取指标信息（支持多关键词，自动分页，并发请求，支持指标ID去重）

        Args:
            keyword: 搜索关键词或关键词列表
            dataset_ids: 数据集ID列表
            fuzzy_match: 是否模糊匹配
            page_size: 每页大小
            max_pages: 最大页数限制，防止无限请求
            extract_rows: 是否只提取rows部分数据
            max_concurrent: 最大并发请求数
            deduplicate_by_metric_id: 是否根据指标ID去重

        Returns:
            Union[Dict, List[Dict]]: 合并后的完整响应或只包含所有rows的列表（去重后）
        """
        logger.info(f"\n=== 根据指标名称搜索指标信息 ===")
        logger.info(f"dataset_ids: {dataset_ids}")
        # 转换单个关键词为列表
        if isinstance(keyword, str):
            keywords = [keyword]
        else:
            keywords = keyword

        all_results = []
        metric_id_set = set()  # 用于跟踪已见过的metricId

        try:
            # 为每个关键词创建异步任务
            for kw in keywords:
                logger.info(f"正在处理指标信息关键词: {kw}")

                # 第一次请求，获取总数
                first_result = await self._make_async_request(
                    "POST",
                    self.api_paths["get_metric_info"],
                    params={"pi": 1, "ps": page_size},
                    data={"keyword": kw, "datasetIds": dataset_ids, "fuzzyMatch": fuzzy_match}
                )

                if str(first_result.get("code", "")) != "0":
                    logger.warning(f"获取关键词'{kw}'的第一页失败: {first_result.get('msg', '未知错误')}")
                    continue

                # 计算总页数
                data = first_result.get("data", {})
                total = int(data.get("total", 0))
                total_pages = (total + page_size - 1) // page_size  # 向上取整

                # 获取第一页结果
                keyword_results = []
                duplicate_count = 0
                if extract_rows:
                    first_page_rows = data.get("rows", [])

                    # 处理第一页结果（如果需要去重）
                    for row in first_page_rows:
                        metric_id = row.get('metricId')
                        if deduplicate_by_metric_id:
                            if metric_id and metric_id in metric_id_set:
                                duplicate_count += 1
                                continue
                            if metric_id:
                                metric_id_set.add(metric_id)

                        keyword_results.append(row)

                    logger.info(
                        f"关键词'{kw}'的第一页返回了{len(first_page_rows)}条结果，去重后保留{len(first_page_rows) - duplicate_count}条")

                    # 打印第一页结果的指标ID和指标名称
                    if keyword_results:
                        for idx, row in enumerate(keyword_results[:min(3, len(keyword_results))]):  # 只打印前3条
                            metric_id = row.get('metricId', 'N/A')
                            metric_name = row.get('metricName', 'N/A')
                            metric_code = row.get('metricCode', 'N/A')
                            logger.info(
                                f"  '{kw}'的结果{idx + 1}: 指标ID={metric_id}, 指标名称={metric_name}, 指标编码={metric_code}")

                        if len(keyword_results) > 3:
                            logger.info(f"  ...以及第一页的其他{len(keyword_results) - 3}条结果")
                else:
                    keyword_results.append(first_result)
                    logger.info(f"关键词'{kw}'的第一页已返回")

                # 限制最大页数
                total_pages = min(total_pages, max_pages)

                if total_pages <= 1:
                    logger.info(f"关键词'{kw}'的总唯一结果数: {len(keyword_results)}条")
                    all_results.extend(keyword_results)
                    continue

                # 创建剩余页面的异步任务
                page_tasks = []
                for page in range(2, total_pages + 1):
                    task = self._make_async_request(
                        "POST",
                        self.api_paths["get_metric_info"],
                        params={"pi": page, "ps": page_size},
                        data={"keyword": kw, "datasetIds": dataset_ids, "fuzzyMatch": fuzzy_match}
                    )
                    page_tasks.append(task)

                # 限制并发请求数
                page_results = []
                for i in range(0, len(page_tasks), max_concurrent):
                    batch = page_tasks[i:i + max_concurrent]
                    batch_results = await asyncio.gather(*batch, return_exceptions=True)
                    page_results.extend(batch_results)

                # 处理结果
                additional_total_rows = 0
                additional_kept_rows = 0
                additional_results = []
                for result in page_results:
                    if isinstance(result, Exception):
                        logger.error(f"关键词'{kw}'的异步请求出错: {str(result)}")
                        continue

                    if str(result.get("code", "")) == "0":
                        if extract_rows:
                            rows = result.get("data", {}).get("rows", [])
                            additional_total_rows += len(rows)

                            # 处理额外页面结果（如果需要去重）
                            for row in rows:
                                metric_id = row.get('metricId')
                                if deduplicate_by_metric_id:
                                    if metric_id and metric_id in metric_id_set:
                                        continue
                                    if metric_id:
                                        metric_id_set.add(metric_id)

                                keyword_results.append(row)
                                additional_results.append(row)
                                additional_kept_rows += 1
                        else:
                            keyword_results.append(result)
                            additional_kept_rows += 1
                            additional_total_rows += 1

                logger.info(
                    f"关键词'{kw}'的额外页面返回了{additional_total_rows}条结果，去重后保留{additional_kept_rows}条")

                # 打印额外页面中的一些结果信息
                if additional_results and extract_rows:
                    sample_size = min(3, len(additional_results))
                    logger.info(f"  关键词'{kw}'的额外页面样本:")
                    for idx, row in enumerate(additional_results[:sample_size]):
                        metric_id = row.get('metricId', 'N/A')
                        metric_name = row.get('metricName', 'N/A')
                        metric_code = row.get('metricCode', 'N/A')
                        logger.info(
                            f"  '{kw}'的额外结果{idx + 1}: 指标ID={metric_id}, 指标名称={metric_name}, 指标编码={metric_code}")

                    if len(additional_results) > 3:
                        logger.info(f"  ...以及额外页面的其他{len(additional_results) - 3}条结果")

                # 统计每个指标ID出现的次数，了解结果分布
                if extract_rows and keyword_results:
                    metric_counts = {}
                    for row in keyword_results:
                        m_id = row.get('metricId')
                        m_name = row.get('metricName', 'N/A')
                        if m_id:
                            if m_id not in metric_counts:
                                metric_counts[m_id] = {'count': 0, 'name': m_name}
                            metric_counts[m_id]['count'] += 1

                    logger.info(f"  关键词'{kw}'的结果分布:")
                    for m_id, info in metric_counts.items():
                        logger.info(f"  - 指标ID={m_id}, 指标名称={info['name']}: {info['count']}条结果")

                logger.info(f"关键词'{kw}'的总唯一结果数: {len(keyword_results)}条")

                # 添加到总结果
                all_results.extend(keyword_results)

            logger.info(f"所有指标信息关键词合并后的总唯一结果数: {len(all_results)}条")

            # 最后检查一遍确保没有重复的metricId（以防万一）
            if deduplicate_by_metric_id and extract_rows:
                final_unique_results = []
                final_metric_id_set = set()
                for result in all_results:
                    metric_id = result.get('metricId')
                    if metric_id and metric_id in final_metric_id_set:
                        continue
                    if metric_id:
                        final_metric_id_set.add(metric_id)
                    final_unique_results.append(result)

                # 如果有发现额外的重复项（不应该发生，但以防万一）
                if len(final_unique_results) < len(all_results):
                    logger.warning(
                        f"最终检查时发现额外的重复项: 移除了{len(all_results) - len(final_unique_results)}条重复结果")
                    all_results = final_unique_results

            return all_results

        except Exception as e:
            logger.error(f"多关键词指标信息搜索过程中出错: {str(e)}")
            raise

    async def get_dimension_info_by_id_async(
            self,
            dimension_ids: Union[str, List[str]],
            max_concurrent: int = 5
    ) -> List[Dict]:
        """
        异步获取维度详情信息（支持单个ID或ID列表，并发请求）

        Args:
            dimension_ids: 单个维度ID或维度ID列表
            max_concurrent: 最大并发请求数

        Returns:
            List[Dict]: 维度详情信息列表
        """
        logger.info(f"\n=== 根据维度ID获取维度详情 ===")

        # 确保API路径已添加
        if "get_dimension_info_by_id" not in self.api_paths:
            self.api_paths["get_dimension_info_by_id"] = "/api/drm/semanticOpenApi/getDimensionInfoById"

        # 转换单个ID为列表
        if isinstance(dimension_ids, str):
            dimension_ids = [dimension_ids]

        all_results = []

        try:
            # 创建异步任务列表
            tasks = []
            for dim_id in dimension_ids:
                logger.info(f"正在准备获取维度ID: {dim_id} 的详情")
                task = self._make_async_request(
                    "POST",
                    self.api_paths["get_dimension_info_by_id"],
                    data={"dimensionId": dim_id}
                )
                tasks.append((dim_id, task))

            # 限制并发请求数
            processed_count = 0
            for i in range(0, len(tasks), max_concurrent):
                batch = tasks[i:i + max_concurrent]
                batch_ids = [dim_id for dim_id, _ in batch]
                batch_tasks = [task for _, task in batch]

                logger.info(
                    f"正在并发获取 {len(batch_ids)} 个维度详情 (ID: {', '.join(batch_ids[:3])}{'...' if len(batch_ids) > 3 else ''})")

                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                # 处理结果
                for idx, (dim_id, result) in enumerate(zip(batch_ids, batch_results)):
                    if isinstance(result, Exception):
                        logger.error(f"维度ID {dim_id} 的请求出错: {str(result)}")
                        continue

                    if str(result.get("code", "")) != "0":
                        logger.warning(f"获取维度ID {dim_id} 失败: {result.get('msg', '未知错误')}")
                        continue

                    # 获取数据部分
                    dimension_info = result.get("data", [])

                    if dimension_info:
                        # 添加原始请求的维度ID，以便跟踪匹配关系
                        for item in dimension_info:
                            if "requested_dimension_id" not in item:
                                item["requested_dimension_id"] = dim_id

                        all_results.extend(dimension_info)
                        logger.info(f"维度ID {dim_id} 返回了 {len(dimension_info)} 条详情信息")

                        # 打印第一条详情的关键信息
                        if dimension_info:
                            first_info = dimension_info[0]
                            dimension_name = first_info.get('dimensionName', 'N/A')
                            dimension_en_name = first_info.get('dimensionEnName', 'N/A')
                            model_name = first_info.get('modelName', 'N/A')
                            logger.info(
                                f"  维度名称={dimension_name}, 英文名称={dimension_en_name}, 模型名称={model_name}")
                    else:
                        logger.warning(f"维度ID {dim_id} 未返回任何详情信息")

                processed_count += len(batch)
                logger.info(f"已处理 {processed_count}/{len(dimension_ids)} 个维度ID")

            # 返回所有结果
            logger.info(f"总共获取到 {len(all_results)} 条维度详情信息")
            return all_results

        except Exception as e:
            logger.error(f"获取维度详情过程中出错: {str(e)}")
            raise

    async def get_model_detail_async(
            self,
            model_ids: Union[str, List[str]],
            max_concurrent: int = 5
    ) -> List[Dict]:
        """
        异步获取模型详情信息（支持单个ID或ID列表，并发请求）

        Args:
            model_ids: 单个模型ID或模型ID列表
            max_concurrent: 最大并发请求数

        Returns:
            List[Dict]: 模型详情信息列表
        """
        logger.info(f"\n=== 根据模型ID获取模型详情 ===")

        # 确保API路径已添加
        if "get_model_detail" not in self.api_paths:
            self.api_paths["get_model_detail"] = "/api/drm/semanticOpenApi/getModelDetail"

        # 转换单个ID为列表
        if isinstance(model_ids, str):
            model_ids = [model_ids]

        all_results = []

        try:
            # 创建异步任务列表
            tasks = []
            for model_id in model_ids:
                logger.info(f"正在准备获取模型ID: {model_id} 的详情")
                task = self._make_async_request(
                    "POST",
                    self.api_paths["get_model_detail"],
                    data={"modelId": model_id}
                )
                tasks.append((model_id, task))

            # 限制并发请求数
            processed_count = 0
            for i in range(0, len(tasks), max_concurrent):
                batch = tasks[i:i + max_concurrent]
                batch_ids = [model_id for model_id, _ in batch]
                batch_tasks = [task for _, task in batch]

                logger.info(
                    f"正在并发获取 {len(batch_ids)} 个模型详情 (ID: {', '.join(batch_ids[:3])}{'...' if len(batch_ids) > 3 else ''})")

                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                # 处理结果
                for idx, (model_id, result) in enumerate(zip(batch_ids, batch_results)):
                    if isinstance(result, Exception):
                        logger.error(f"模型ID {model_id} 的请求出错: {str(result)}")
                        continue

                    if str(result.get("code", "")) != "0":
                        logger.warning(f"获取模型ID {model_id} 失败: {result.get('msg', '未知错误')}")
                        continue

                    # 获取数据部分
                    model_data = result.get("data")

                    if model_data:
                        # 添加原始请求的模型ID，以便跟踪匹配关系
                        if "requested_model_id" not in model_data:
                            model_data["requested_model_id"] = model_id

                        all_results.append(model_data)

                        # 打印模型的关键信息
                        model_name = model_data.get('modelName', 'N/A')
                        table_name = model_data.get('tableName', 'N/A')
                        description = model_data.get('description', 'N/A')
                        fields_count = len(model_data.get('fields', []))
                        datasets = model_data.get('usedInDatasets', [])
                        datasets_names = [d.get('datasetName', 'N/A') for d in datasets]

                        logger.info(
                            f"  模型ID {model_id} 详情: 模型名称={model_name}, 表名={table_name}, 描述={description}")
                        logger.info(
                            f"  字段数量: {fields_count}, 用于数据集: {', '.join(datasets_names[:3])}{'...' if len(datasets_names) > 3 else ''}")
                    else:
                        logger.warning(f"模型ID {model_id} 未返回任何详情信息")

                processed_count += len(batch)
                logger.info(f"已处理 {processed_count}/{len(model_ids)} 个模型ID")

            # 返回所有结果
            logger.info(f"总共获取到 {len(all_results)} 条模型详情信息")
            return all_results

        except Exception as e:
            logger.error(f"获取模型详情过程中出错: {str(e)}")
            raise

    async def get_model_relationships_async(
            self,
            model_ids: Union[str, List[str]],
            max_concurrent: int = 5
    ) -> List[Dict]:
        """
        异步获取模型关系信息（支持单个ID或ID列表，并发请求，根据模型关系去重）

        Args:
            model_ids: 单个模型ID或模型ID列表
            max_concurrent: 最大并发请求数

        Returns:
            List[Dict]: 去重后的模型关系信息列表
        """
        logger.info(f"\n=== 根据模型ID获取模型关系 ===")

        # 确保API路径已添加
        if "get_model_relationships" not in self.api_paths:
            self.api_paths["get_model_relationships"] = "/api/drm/semanticOpenApi/getModelRelationships"

        # 转换单个ID为列表
        if isinstance(model_ids, str):
            model_ids = [model_ids]

        all_relationships = []
        relationship_keys = set()  # 用于跟踪已见过的关系组合

        try:
            # 创建异步任务列表
            tasks = []
            for model_id in model_ids:
                logger.info(f"正在准备获取模型ID: {model_id} 的关系信息")
                task = self._make_async_request(
                    "POST",
                    self.api_paths["get_model_relationships"],
                    data={"modelIds": [model_id]}  # API要求数组，虽然每次只传一个ID
                )
                tasks.append((model_id, task))

            # 限制并发请求数
            processed_count = 0
            for i in range(0, len(tasks), max_concurrent):
                batch = tasks[i:i + max_concurrent]
                batch_ids = [model_id for model_id, _ in batch]
                batch_tasks = [task for _, task in batch]

                logger.info(
                    f"正在并发获取 {len(batch_ids)} 个模型的关系信息 (ID: {', '.join(batch_ids[:3])}{'...' if len(batch_ids) > 3 else ''})")

                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                # 处理结果
                for idx, (model_id, result) in enumerate(zip(batch_ids, batch_results)):
                    if isinstance(result, Exception):
                        logger.error(f"模型ID {model_id} 的关系请求出错: {str(result)}")
                        continue

                    if str(result.get("code", "")) != "0":
                        logger.warning(f"获取模型ID {model_id} 的关系失败: {result.get('msg', '未知错误')}")
                        continue

                    # 获取数据部分
                    relationships = result.get("data", [])

                    if relationships:
                        added_count = 0
                        for relation in relationships:
                            # 添加原始请求的模型ID，以便跟踪匹配关系
                            if "requested_model_id" not in relation:
                                relation["requested_model_id"] = model_id

                            # 根据sourceModelId和targetModelId组合创建唯一键
                            source_id = relation.get('sourceModelId')
                            target_id = relation.get('targetModelId')
                            if not source_id or not target_id:
                                logger.warning(f"关系数据缺少source或target模型ID: {relation}")
                                continue

                            # 创建一个排序后的键，确保无论方向如何都能识别相同的关系
                            relation_key = tuple(sorted([source_id, target_id]) +
                                                 [relation.get('sourceField', ''), relation.get('targetField', '')])

                            # 检查是否已经存在相同的关系
                            if relation_key in relationship_keys:
                                continue

                            # 添加关系并记录键
                            relationship_keys.add(relation_key)
                            all_relationships.append(relation)
                            added_count += 1

                        # 打印模型关系信息
                        total_relations = len(relationships)
                        logger.info(
                            f"  模型ID {model_id} 返回了 {total_relations} 条关系信息，去重后新增 {added_count} 条")

                        # 打印部分关系示例
                        if added_count > 0:
                            sample_size = min(3, added_count)
                            sample_relations = [r for r in all_relationships[-added_count:]][:sample_size]

                            for i, relation in enumerate(sample_relations):
                                source_name = relation.get('sourceModelName', 'N/A')
                                target_name = relation.get('targetModelName', 'N/A')
                                source_field = relation.get('sourceField', 'N/A')
                                target_field = relation.get('targetField', 'N/A')
                                join_type = relation.get('joinType', 'N/A')

                                logger.info(
                                    f"    关系{i + 1}: {source_name}({source_field}) {join_type} JOIN {target_name}({target_field})")
                    else:
                        logger.info(f"  模型ID {model_id} 未返回任何关系信息")

                processed_count += len(batch)
                logger.info(f"已处理 {processed_count}/{len(model_ids)} 个模型ID")

            # 返回所有去重后的结果
            logger.info(f"总共获取到 {len(all_relationships)} 条唯一的模型关系信息")
            return all_relationships

        except Exception as e:
            logger.error(f"获取模型关系过程中出错: {str(e)}")
            raise

    async def get_dataset_detail_async(
            self,
            dataset_ids: Union[str, List[str]],
            max_concurrent: int = 5
    ) -> List[Dict]:
        """
        异步获取数据集详情信息（支持单个ID或ID列表，并发请求）

        Args:
            dataset_ids: 单个数据集ID或数据集ID列表
            max_concurrent: 最大并发请求数

        Returns:
            List[Dict]: 数据集详情信息列表
        """
        logger.info(f"\n=== 根据数据集ID获取数据集详情 ===")

        # 确保API路径已添加
        if "get_dataset_detail" not in self.api_paths:
            self.api_paths["get_dataset_detail"] = "/api/drm/semanticOpenApi/getDatasetDetail"

        # 转换单个ID为列表
        if isinstance(dataset_ids, str):
            dataset_ids = [dataset_ids]

        all_results = []

        try:
            # 创建异步任务列表
            tasks = []
            for dataset_id in dataset_ids:
                logger.info(f"正在准备获取数据集ID: {dataset_id} 的详情")
                task = self._make_async_request(
                    "POST",
                    self.api_paths["get_dataset_detail"],
                    data={"datasetId": dataset_id}
                )
                tasks.append((dataset_id, task))

            # 限制并发请求数
            processed_count = 0
            for i in range(0, len(tasks), max_concurrent):
                batch = tasks[i:i + max_concurrent]
                batch_ids = [dataset_id for dataset_id, _ in batch]
                batch_tasks = [task for _, task in batch]

                logger.info(
                    f"正在并发获取 {len(batch_ids)} 个数据集详情 (ID: {', '.join(batch_ids[:3])}{'...' if len(batch_ids) > 3 else ''})")

                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                # 处理结果
                for idx, (dataset_id, result) in enumerate(zip(batch_ids, batch_results)):
                    if isinstance(result, Exception):
                        logger.error(f"数据集ID {dataset_id} 的请求出错: {str(result)}")
                        continue

                    if str(result.get("code", "")) != "0":
                        logger.warning(f"获取数据集ID {dataset_id} 失败: {result.get('msg', '未知错误')}")
                        continue

                    # 获取数据部分
                    dataset_data = result.get("data")

                    if dataset_data:
                        # 添加原始请求的数据集ID，以便跟踪匹配关系
                        if "requested_dataset_id" not in dataset_data:
                            dataset_data["requested_dataset_id"] = dataset_id

                        # 去除重复的模型（如示例中显示的可能有重复）
                        if "models" in dataset_data and dataset_data["models"]:
                            unique_models = []
                            model_ids_seen = set()
                            for model in dataset_data["models"]:
                                model_id = model.get("modelId")
                                if model_id and model_id not in model_ids_seen:
                                    model_ids_seen.add(model_id)
                                    unique_models.append(model)
                            dataset_data["models"] = unique_models

                        all_results.append(dataset_data)

                        # 只打印最简单的数据集信息：ID和名称
                        dataset_name = dataset_data.get('datasetName', 'N/A')
                        logger.info(f"  已获取数据集: ID={dataset_id}, 名称={dataset_name}")
                    else:
                        logger.warning(f"数据集ID {dataset_id} 未返回任何详情信息")

                processed_count += len(batch)
                logger.info(f"已处理 {processed_count}/{len(dataset_ids)} 个数据集ID")

            # 返回所有结果
            logger.info(f"总共获取到 {len(all_results)} 条数据集详情信息")
            return all_results

        except Exception as e:
            logger.error(f"获取数据集详情过程中出错: {str(e)}")
            raise

    async def get_business_term_info_async(
            self,
            keyword: Union[str, List[str]],
            domain_ids: List[str],
            fuzzy_match: bool = True,
            page_size: int = 100,
            max_pages: int = 10,
            extract_rows: bool = True,
            max_concurrent: int = 5
    ) -> Union[Dict, List[Dict]]:
        """
        异步获取业务术语信息（支持多关键词，自动分页，并发请求）

        Args:
            keyword: 搜索关键词或关键词列表
            domain_ids: 主题域ID列表
            fuzzy_match: 是否模糊匹配
            page_size: 每页大小
            max_pages: 最大页数限制，防止无限请求
            extract_rows: 是否只提取rows部分数据
            max_concurrent: 最大并发请求数

        Returns:
            Union[Dict, List[Dict]]: 合并后的完整响应或只包含所有rows的列表
        """
        logger.info(f"\n=== 根据关键词搜索业务术语 ===")
        logger.info(f"domain_ids: {domain_ids}")

        # 转换单个关键词为列表
        if isinstance(keyword, str):
            keywords = [keyword]
        else:
            keywords = keyword

        all_results = []

        try:
            # 为每个关键词创建异步任务
            for kw in keywords:
                logger.info(f"正在处理业务术语关键词: {kw}")

                # 第一次请求，获取总数
                first_result = await self._make_async_request(
                    "POST",
                    self.api_paths["get_business_term_info"],
                    params={"pi": 1, "ps": page_size},
                    data={"keyword": kw, "domainIds": domain_ids, "fuzzyMatch": fuzzy_match}
                )

                if str(first_result.get("code", "")) != "0":
                    logger.warning(f"获取关键词'{kw}'的第一页失败: {first_result.get('msg', '未知错误')}")
                    continue

                # 计算总页数
                data = first_result.get("data", {})
                total = int(data.get("total", 0))
                total_pages = (total + page_size - 1) // page_size  # 向上取整

                # 获取第一页结果
                keyword_results = []
                if extract_rows:
                    first_page_rows = data.get("rows", [])
                    keyword_results.extend(first_page_rows)
                    logger.info(f"关键词'{kw}'的第一页返回了{len(first_page_rows)}条结果")
                else:
                    keyword_results.append(first_result)
                    logger.info(f"关键词'{kw}'的第一页已返回")

                # 限制最大页数
                total_pages = min(total_pages, max_pages)

                if total_pages <= 1:
                    logger.info(f"关键词'{kw}'的总结果数: {len(keyword_results)}条")
                    all_results.extend(keyword_results)
                    continue

                # 创建剩余页面的异步任务
                page_tasks = []
                for page in range(2, total_pages + 1):
                    task = self._make_async_request(
                        "POST",
                        self.api_paths["get_business_term_info"],
                        params={"pi": page, "ps": page_size},
                        data={"keyword": kw, "domainIds": domain_ids, "fuzzyMatch": fuzzy_match}
                    )
                    page_tasks.append(task)

                # 限制并发请求数
                page_results = []
                for i in range(0, len(page_tasks), max_concurrent):
                    batch = page_tasks[i:i + max_concurrent]
                    batch_results = await asyncio.gather(*batch, return_exceptions=True)
                    page_results.extend(batch_results)

                # 处理结果
                additional_rows_count = 0
                for result in page_results:
                    if isinstance(result, Exception):
                        logger.error(f"关键词'{kw}'的异步请求出错: {str(result)}")
                        continue

                    if str(result.get("code", "")) != "0":
                        logger.warning(f"关键词'{kw}'的请求返回错误: {result.get('msg', '未知错误')}")
                        continue

                    if extract_rows:
                        rows = result.get("data", {}).get("rows", [])
                        keyword_results.extend(rows)
                        additional_rows_count += len(rows)
                    else:
                        keyword_results.append(result)
                        additional_rows_count += 1

                logger.info(f"关键词'{kw}'的额外页面返回了{additional_rows_count}条结果")
                logger.info(f"关键词'{kw}'的总结果数: {len(keyword_results)}条")

                # 添加到总结果
                all_results.extend(keyword_results)

            logger.info(f"所有业务术语关键词合并后的总结果数: {len(all_results)}条")
            return all_results

        except Exception as e:
            logger.error(f"业务术语搜索过程中出错: {str(e)}")
            raise


# 使用示例
if __name__ == "__main__":
    # 创建客户端实例
    client = SemanticApiClient(
        protocol=DCS_SERVER_PROTOCOL,
        host=DCS_SERVER_HOST,
        port=DCS_SERVER_PORT
    )


    # 异步请求示例
    async def run_async_examples():
        try:
            # 1. 获取维度信息（自动分页）
            dimension_rows = await client.get_dimension_info_by_keyword_async(
                keyword="职称",
                dataset_ids=["35799132679879680"],
                fuzzy_match=True
            )
            print(f"\n获取维度信息结果行数: {len(dimension_rows)}")

            # 打印第一行数据示例
            if dimension_rows:
                print(f"第一条维度信息: {json.dumps(dimension_rows[0], ensure_ascii=False)}")

            # 2. 搜索维度值（自动分页）
            dimension_value_rows = await client.get_dimension_by_dimension_value_async(
                keyword="教授",
                dataset_ids=["35799132679879680"],
                fuzzy_match=True
            )
            print(f"\n搜索维度值结果行数: {len(dimension_value_rows)}")

            # 打印第一行数据示例
            if dimension_value_rows:
                print(f"第一条维度值: {json.dumps(dimension_value_rows[0], ensure_ascii=False)}")

            # 3. 获取指标信息（自动分页）
            metric_rows = await client.get_metric_info_by_keyword_async(
                keyword="总课时数",
                dataset_ids=["35799132679879680"],
                fuzzy_match=True
            )
            print(f"\n获取指标信息结果行数: {len(metric_rows)}")

            # 打印第一行数据示例
            if metric_rows:
                print(f"第一条指标信息: {json.dumps(metric_rows[0], ensure_ascii=False)}")

        except Exception as e:
            print(f"异步请求发生错误: {e}")


    # 运行异步示例
    if asyncio.get_event_loop().is_closed():
        asyncio.set_event_loop(asyncio.new_event_loop())
    asyncio.get_event_loop().run_until_complete(run_async_examples())
