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
            "get_model_detail": "/api/drm/semanticOpenApi/getModelDetail"
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

    async def get_business_term_info_async(
            self,
            keyword: str,
            domain_ids: List[str],
            fuzzy_match: bool = True,
            page_size: int = 100,
            max_pages: int = 10,
            extract_rows: bool = True,
            max_concurrent: int = 5
    ) -> Union[Dict, List[Dict]]:
        """
        异步获取业务术语信息（自动分页，并发请求）

        Args:
            keyword: 搜索关键词
            domain_ids: 主题域ID列表
            fuzzy_match: 是否模糊匹配
            page_size: 每页大小
            max_pages: 最大页数限制，防止无限请求
            extract_rows: 是否只提取rows部分数据
            max_concurrent: 最大并发请求数

        Returns:
            Union[Dict, List[Dict]]: 合并后的完整响应或只包含所有rows的列表
        """
        try:
            # 第一次请求，获取总数
            first_result = await self._make_async_request(
                "POST",
                self.api_paths["get_business_term_info"],
                params={"pi": 1, "ps": page_size},
                data={"keyword": keyword, "domainIds": domain_ids, "fuzzyMatch": fuzzy_match}
            )

            if str(first_result.get("code", "")) != "0":
                logger.warning(f"Failed to get first page: {first_result.get('msg', 'Unknown error')}")
                return [] if extract_rows else first_result

            # 计算总页数
            data = first_result.get("data", {})
            total = int(data.get("total", 0))
            total_pages = (total + page_size - 1) // page_size  # 向上取整

            # 限制最大页数
            total_pages = min(total_pages, max_pages)

            # 保存第一页结果
            all_results = []
            if extract_rows:
                all_results.extend(data.get("rows", []))
            else:
                all_results.append(first_result)

            if total_pages <= 1:
                return all_results

            # 创建剩余页面的异步任务
            tasks = []
            for page in range(2, total_pages + 1):
                task = self._make_async_request(
                    "POST",
                    self.api_paths["get_business_term_info"],
                    params={"pi": page, "ps": page_size},
                    data={"keyword": keyword, "domainIds": domain_ids, "fuzzyMatch": fuzzy_match}
                )
                tasks.append(task)

            # 限制并发请求数
            results = []
            for i in range(0, len(tasks), max_concurrent):
                batch = tasks[i:i + max_concurrent]
                batch_results = await asyncio.gather(*batch, return_exceptions=True)
                results.extend(batch_results)

            # 处理结果
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Error in async request: {str(result)}")
                    continue

                if str(result.get("code", "")) == "0":
                    if extract_rows:
                        all_results.extend(result.get("data", {}).get("rows", []))
                    else:
                        all_results.append(result)

            return all_results

        except Exception as e:
            logger.error(f"Error in async auto-pagination: {str(e)}")
            raise

    async def get_model_detail_async(self, model_id: str) -> Dict:
        """
        异步获取模型详情

        Args:
            model_id: 模型ID

        Returns:
            Dict: 模型详情信息
        """
        try:
            logger.info(f"Getting model detail for model ID: {model_id}")

            # 发送请求获取模型详情
            result = await self._make_async_request(
                "POST",
                self.api_paths["get_model_detail"],
                data={"modelId": model_id}
            )

            if str(result.get("code", "")) != "0":
                logger.warning(f"Failed to get model detail: {result.get('msg', 'Unknown error')}")
                return {}

            return result.get("data", {})

        except Exception as e:
            logger.error(f"Error in get_model_detail_async: {str(e)}")
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
