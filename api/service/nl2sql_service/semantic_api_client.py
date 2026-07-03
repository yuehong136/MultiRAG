#!/usr/bin/env python

"""
Enhanced API Client for DRM Semantic OpenAPI
用于向DRM语义化API服务发送请求的客户端（带自动分页和异步功能）
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import urljoin

import aiohttp
import requests

from api.service.askdata_service.event.event_utils import send_event
from common.settings import DCS_SEMANTIC_SERVER_ACCESS_KEY, DCS_SEMANTIC_SERVER_SECRET_KEY, DCS_SERVER_HOST, DCS_SERVER_PORT, DCS_SERVER_PROTOCOL

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 语义层出站请求【全局并发限流】
#
# 背景：智能问数一次问题会对中台语义层发起"扇出式"并发请求——按模型并发拉
#   dims/metrics、拉模型关系、按关键字四件套(×分词×分页)等。本 client 过去每次调用
#   都新建 aiohttp.ClientSession 且无任何并发上限，几个问题并发就可能瞬间打出几十个
#   并发请求，压垮中台的 HikariCP 连接池 / Tomcat 线程，让本来 3~10ms 的快接口
#   (如 getModelIndsAndDimsByModelId)排队拿不到连接 >30s 而超时（已在演示库实测：
#   该 SQL 本身仅 9.6ms、PG 端 1000 连接仅用 139、无慢查询 → 瓶颈在中台共享池而非 SQL）。
#
#   这里在【唯一出站收口】_make_async_request 上加一道信号量，把"引擎→中台"的总并发
#   钳在 SEMANTIC_API_MAX_CONCURRENCY 以内（默认 8，可用同名环境变量调整）：
#     · 命中少数模型的常规问题（在途数 ≤ 上限）完全不受影响、仍并行；
#     · 扇出风暴被削平为"至多 N 个在途、其余排队"，不再自伤式压垮中台池。
#
#   ⚠ asyncio 信号量与事件循环绑定：按【运行中的 loop 懒加载】，避免多事件循环
#     (测试/脚本反复 asyncio.run) 下 "Future attached to a different loop"。生产是
#     单进程单 loop，等价于一个进程级全局限流器；多个 SemanticApiClient 实例共享同一
#     loop 上的同一信号量，故是【全局并发上限】而非每实例各算各的。
# ---------------------------------------------------------------------------
SEMANTIC_API_MAX_CONCURRENCY = max(1, int(os.getenv("SEMANTIC_API_MAX_CONCURRENCY", "8")))

_semaphores_by_loop: dict = {}  # {event_loop: asyncio.Semaphore}


def _get_semantic_api_semaphore() -> asyncio.Semaphore:
    """取当前事件循环对应的出站并发信号量（懒加载、按 loop 隔离）。仅在 async 上下文调用。"""
    loop = asyncio.get_running_loop()
    sem = _semaphores_by_loop.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(SEMANTIC_API_MAX_CONCURRENCY)
        _semaphores_by_loop[loop] = sem
    return sem


# 自定义异常类
class SemanticApiError(Exception):
    """DRM语义化API请求错误的基类异常"""
    pass


class ApiRequestError(SemanticApiError):
    """API请求发送错误"""
    pass


class ApiResponseError(SemanticApiError):
    """API响应错误（业务状态码非0）"""
    pass


class ApiNetworkError(SemanticApiError):
    """网络连接错误"""
    pass


class ApiJsonDecodeError(SemanticApiError):
    """响应JSON解析错误"""
    pass


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
            "get_metric_info_by_id": "/api/drm/semanticOpenApi/getMetricInfoByKId",
            "get_dimension_values": "/api/drm/semanticOpenApi/getDimensionValues",
            "get_model_relationships": "/api/drm/semanticOpenApi/getModelRelationships",
            "get_dataset_detail": "/api/drm/semanticOpenApi/getDatasetDetail",
            "get_model_inds_and_dims": "/api/drm/semanticOpenApi/getModelIndsAndDimsByModelId",
            "get_hc_dimension_by_dimension_value": "/api/drm/semanticOpenApi/getHCDimensionByDimensionValue",
            "get_hc_dim_values_by_dim_value": "/api/drm/semanticOpenApi/getHCDimValuesByDimValue",
            "get_user_semantic_permissions": "/api/drm/semanticOpenApi/getUserSemanticPermissions",
        }

        # 设置请求头
        self.headers = {
            "Content-Type": "application/json"
        }
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    def _generate_signature(self) -> dict[str, str]:
        """生成API请求签名"""
        timestamp = str(int(time.time()))
        access_key = DCS_SEMANTIC_SERVER_ACCESS_KEY
        secret_key = DCS_SEMANTIC_SERVER_SECRET_KEY

        if not access_key or not secret_key:
            return {}

        # Using HMAC-SHA256 for the signature
        message = f"{access_key}{timestamp}".encode()
        secret = secret_key.encode('utf-8')

        signature = hmac.new(secret, message, digestmod=hashlib.sha256).hexdigest()

        return {
            "accessKey": access_key,
            "timestamp": timestamp,
            "signature": signature
        }
    def _make_request(
            self,
            method: str,
            api_path: str,
            params: dict = None,
            data: dict = None
    ) -> dict:
        """
        发送请求并处理响应

        Args:
            method: 请求方法 (GET, POST, PUT, DELETE等)
            api_path: API路径
            params: URL参数
            data: 请求体数据

        Returns:
            Dict: API响应结果

        Raises:
            ApiNetworkError: 网络连接错误
            ApiJsonDecodeError: JSON解析错误
            ApiResponseError: API响应业务状态码不为0
            ApiRequestError: 其他API请求错误
        """
        url = urljoin(self.base_url, api_path)

        request_headers = self.headers.copy()
        signature_headers = self._generate_signature()
        request_headers.update(signature_headers)

        try:
            logger.info(f"发送 {method} 请求到 {url}")
            if data:
                logger.debug(f"请求数据: {json.dumps(data, ensure_ascii=False)}")

            response = requests.request(
                method=method,
                url=url,
                params=params,
                json=data,
                headers=request_headers,
                timeout=self.timeout
            )

            # 检查HTTP状态码
            response.raise_for_status()

            # 解析响应JSON
            result = response.json()
            logger.debug(f"响应: {json.dumps(result, ensure_ascii=False)}")

            # 检查业务状态码
            if "code" in result and str(result["code"]) != "0":
                error_msg = result.get('msg', '未知错误')
                logger.warning(f"业务错误: {error_msg}")
                raise ApiResponseError(f"业务错误(code={result['code']}): {error_msg}")

            return result

        except requests.exceptions.RequestException as e:
            error_msg = f"请求错误: {e!s}"
            logger.error(error_msg)
            raise ApiNetworkError(error_msg) from e
        except json.JSONDecodeError as e:
            error_msg = f"JSON解析错误: {e!s}"
            logger.error(error_msg)
            raise ApiJsonDecodeError(error_msg) from e
        except Exception as e:
            if isinstance(e, SemanticApiError):
                raise  # 如果是已经包装过的异常就直接抛出
            error_msg = f"意外错误: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def _make_async_request(
            self,
            method: str,
            api_path: str,
            params: dict = None,
            data: dict = None
    ) -> dict:
        """
        异步发送请求并处理响应

        Args:
            method: 请求方法 (GET, POST, PUT, DELETE等)
            api_path: API路径
            params: URL参数
            data: 请求体数据

        Returns:
            Dict: API响应结果

        Raises:
            ApiNetworkError: 网络连接错误
            ApiJsonDecodeError: JSON解析错误
            ApiResponseError: API响应业务状态码不为0
            ApiRequestError: 其他API请求错误
        """
        url = urljoin(self.base_url, api_path)

        request_headers = self.headers.copy()
        signature_headers = self._generate_signature()
        request_headers.update(signature_headers)

        try:
            logger.info(f"异步发送 {method} 请求到 {url}")
            if data:
                logger.debug(f"异步请求数据: {json.dumps(data, ensure_ascii=False)}")

            # 出站并发限流：抢一个槽位再发请求。槽位用尽时在此排队（不计入下面的
            # aiohttp 超时——total 只从 session.request 开始计时）。排队明显时打一条
            # 告警，既是自我保护、也是"扇出风暴/中台变慢"的现场信号（会落到每问日志）。
            sem = _get_semantic_api_semaphore()
            _wait_start = time.monotonic()
            async with sem:
                _waited = time.monotonic() - _wait_start
                if _waited > 0.5:
                    logger.warning(
                        f"语义层出站并发已达上限({SEMANTIC_API_MAX_CONCURRENCY})，本次请求排队 "
                        f"{_waited:.2f}s 后才发出: {method} {api_path}"
                    )
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                            method=method,
                            url=url,
                            params=params,
                            json=data,
                            headers=request_headers,
                            timeout=aiohttp.ClientTimeout(total=self.timeout)
                    ) as response:
                        # 检查HTTP状态码
                        response.raise_for_status()

                        # 解析响应JSON
                        result = await response.json()
                        logger.debug(f"异步响应: {json.dumps(result, ensure_ascii=False)}")

                        # 检查业务状态码
                        if "code" in result and str(result["code"]) != "0":
                            error_msg = result.get('msg', '未知错误')
                            logger.warning(f"业务错误: {error_msg}")
                            raise ApiResponseError(f"业务错误(code={result['code']}): {error_msg}")

                        return result

        except TimeoutError:
            error_msg = f"异步请求超时({self.timeout}s): {method} {url}"
            logger.error(error_msg)
            raise ApiNetworkError(error_msg)
        except aiohttp.ClientError as e:
            error_msg = f"异步请求错误: {type(e).__name__}: {e}"
            logger.error(error_msg)
            raise ApiNetworkError(error_msg) from e
        except json.JSONDecodeError as e:
            error_msg = f"异步JSON解析错误: {e!s}"
            logger.error(error_msg)
            raise ApiJsonDecodeError(error_msg) from e
        except Exception as e:
            if isinstance(e, SemanticApiError):
                raise
            error_msg = f"异步请求异常: {type(e).__name__}: {e}, url={url}"
            logger.error(error_msg, exc_info=True)
            raise ApiRequestError(error_msg) from e

    async def get_dimension_info_by_keyword_async(
            self,
            keyword: str | list[str],
            dataset_ids: list[str],
            fuzzy_match: bool = True,
            page_size: int = 100,
            max_pages: int = 100,
            extract_rows: bool = True,
            max_concurrent: int = 5,
            deduplicate_by_dimension_id: bool = True,
            event_id: str | None = None
    ) -> dict | list[dict]:
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

        Raises:
            SemanticApiError: 请求或处理过程中的任何错误
        """
        logger.info("\n=== 根据维度名称搜索维度信息 ===")
        logger.info(f"dataset_ids: {dataset_ids}")
        if event_id:
            await send_event(event_id, {"task_name": "分词获取维度信息", "task_status": "working"}, "task")
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

                try:
                    # 第一次请求，获取总数
                    first_result = await self._make_async_request(
                        "POST",
                        self.api_paths["get_dimension_info"],
                        params={"pi": 1, "ps": page_size},
                        data={"keyword": kw, "datasetIds": dataset_ids, "fuzzyMatch": fuzzy_match}
                    )

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
                            error_msg = f"关键词'{kw}'的异步请求出错: {result!s}"
                            logger.error(error_msg)
                            raise ApiRequestError(error_msg) from result

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

                except SemanticApiError as e:
                    # 某个关键词处理失败，但允许继续处理其他关键词
                    logger.error(f"处理关键词 '{kw}' 时出错: {e!s}")
                    raise

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

            if event_id:
                await send_event(event_id, {"task_name": "分词获取维度信息", "task_data": {
                    "dimension_info_list": [
                        {
                            "dimension_id": item.get("dimensionId"),
                            "dimension_name": item.get("dimensionName"),
                            "model_name": item.get("modelName", None)
                        }
                        for item in all_results
                    ]
                }}, "task_data")
                await send_event(event_id, {"task_name": "分词获取维度信息", "task_status": "completed"}, "task")
                await send_event(event_id, {}, "progress_up")

            return all_results

        except Exception as e:
            if not isinstance(e, SemanticApiError):
                error_msg = f"多关键词维度信息搜索过程中出错: {e!s}"
                logger.error(error_msg)
                raise ApiRequestError(error_msg) from e
            raise  # 重新抛出已经包装的异常

    async def get_dimension_by_dimension_value_async(
            self,
            keyword: str | list[str],
            dataset_ids: list[str],
            fuzzy_match: bool = True,
            page_size: int = 100,
            max_pages: int = 100,
            extract_rows: bool = True,
            max_concurrent: int = 5,
            deduplicate_by_dimension_id: bool = True,
            event_id: str | None = None
    ) -> dict | list[dict]:
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

        Raises:
            ApiResponseError: API响应业务状态码不为0
            ApiRequestError: 请求过程中的其他错误
        """
        logger.info("\n=== 根据维度值名称搜索维度信息 ===")
        logger.info(f"dataset_ids: {dataset_ids}")
        if event_id:
            await send_event(event_id, {"task_name": "维度值匹配维度", "task_status": "working"}, "task")
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

                try:
                    # 第一次请求，获取总数
                    first_result = await self._make_async_request(
                        "POST",
                        self.api_paths["get_dimension_by_dimension_value"],
                        params={"pi": 1, "ps": page_size},
                        data={"keyword": kw, "datasetIds": dataset_ids, "fuzzyMatch": fuzzy_match}
                    )

                    if str(first_result.get("code", "")) != "0":
                        error_msg = f"获取关键词'{kw}'的第一页失败: {first_result.get('msg', '未知错误')}"
                        logger.warning(error_msg)
                        raise ApiResponseError(error_msg)

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
                                logger.info(
                                    f"  '{kw}'的结果{idx + 1}: 维度ID={dimension_id}, 维度名称={dimension_name}")

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
                            error_msg = f"关键词'{kw}'的异步请求出错: {result!s}"
                            logger.error(error_msg)
                            raise ApiRequestError(error_msg) from result

                        if str(result.get("code", "")) != "0":
                            error_msg = f"关键词'{kw}'的请求返回错误: {result.get('msg', '未知错误')}"
                            logger.warning(error_msg)
                            raise ApiResponseError(error_msg)

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
                            logger.info(
                                f"  '{kw}'的额外结果{idx + 1}: 维度ID={dimension_id}, 维度名称={dimension_name}")

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

                except SemanticApiError as e:
                    # 记录错误但继续处理其他关键词
                    logger.error(f"处理关键词 '{kw}' 时出错: {e!s}")
                    raise  # 重新抛出异常

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

            if event_id:
                await send_event(event_id, {"task_name": "维度值匹配维度", "task_data": {
                    "dimension_info_list": [
                        {
                            "dimension_id": item.get("dimensionId"),
                            "dimension_name": item.get("dimensionName"),
                            "model_name": item.get("modelName", None)
                        }
                        for item in all_results
                    ]
                }}, "task_data")
                await send_event(event_id, {"task_name": "维度值匹配维度", "task_status": "completed"}, "task")
                await send_event(event_id, {}, "progress_up")
            return all_results

        except Exception as e:
            if not isinstance(e, SemanticApiError):
                error_msg = f"多关键词维度值搜索过程中出错: {e!s}"
                logger.error(error_msg)
                raise ApiRequestError(error_msg) from e
            raise  # 重新抛出已经包装的异常

    async def get_hc_dimension_by_dimension_value_async(
            self,
            keyword_list: list[str],
            dataset_ids: list[str],
            exclude_dim_ids: list[str] = None,
            fuzzy_match: bool = True,
            event_id: str | None = None
    ) -> list[dict]:
        """
        异步获取HC维度信息（通过维度值搜索）
        """
        logger.info("\n=== 通过维度值搜索HC维度信息 ===")
        logger.info(f"关键词列表: {keyword_list}")
        logger.info(f"数据集ID: {dataset_ids}")
        logger.info(f"排除维度ID: {exclude_dim_ids}")
        logger.info(f"模糊匹配: {fuzzy_match}")
        if event_id:
            await send_event(event_id, {"task_name": "维度值匹配高基数维度", "task_status": "working"}, "task")

        # 参数验证
        if not keyword_list:
            error_msg = "关键词列表不能为空"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        if not dataset_ids:
            error_msg = "数据集ID列表不能为空"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        try:
            # 发起请求
            result = await self._make_async_request(
                "POST",
                self.api_paths["get_hc_dimension_by_dimension_value"],
                data={
                    "keywordList": keyword_list,
                    "datasetIds": dataset_ids,
                    "excludeDimIds": exclude_dim_ids,
                    "fuzzyMatch": fuzzy_match
                }
            )

            # 获取数据部分
            hc_dimensions = result.get("data", [])

            if hc_dimensions:
                logger.info(f"成功获取到 {len(hc_dimensions)} 条HC维度信息")

                # 打印结果详情
                for idx, dimension in enumerate(hc_dimensions):
                    dimension_id = dimension.get('dimensionId', 'N/A')
                    dimension_name = dimension.get('dimensionName', 'N/A')
                    dimension_en_name = dimension.get('dimensionEnName', 'N/A')
                    dataobject = dimension.get('dataobject', 'N/A')
                    matched_keywords = dimension.get('matched', [])

                    logger.info(
                        f"  结果 {idx + 1}: 维度ID={dimension_id}, 中文名={dimension_name}, 英文名={dimension_en_name}")
                    logger.info(f"    数据对象={dataobject}, 匹配关键词={matched_keywords}")
            else:
                logger.info("未获取到任何HC维度信息")
            if event_id:
                await send_event(event_id, {"task_name": "维度值匹配高基数维度", "task_data": {
                    "dimension_info_list": [
                        {
                            "dimension_id": item.get("dimensionId"),
                            "dimension_name": item.get("dimensionName"),
                            "model_name": item.get("modelName", None)
                        }
                        for item in hc_dimensions
                    ]
                }}, "task_data")
                await send_event(event_id, {"task_name": "维度值匹配高基数维度", "task_status": "completed"}, "task")
                await send_event(event_id, {}, "progress_up")
            return hc_dimensions

        except SemanticApiError as e:
            # 如果是已经封装好的API异常则直接抛出
            raise e
        except Exception as e:
            # 其他异常封装为ApiRequestError后抛出
            error_msg = f"获取HC维度信息过程中出错: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def get_metric_info_by_keyword_async(
            self,
            keyword: str | list[str],
            dataset_ids: list[str],
            fuzzy_match: bool = True,
            page_size: int = 100,
            max_pages: int = 100,
            extract_rows: bool = True,
            max_concurrent: int = 5,
            deduplicate_by_metric_id: bool = True,
            event_id: str | None = None
    ) -> dict | list[dict]:
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

        Raises:
            ApiResponseError: API响应业务状态码不为0
            ApiRequestError: 请求过程中的其他错误
        """
        logger.info("\n=== 根据指标名称搜索指标信息 ===")
        logger.info(f"dataset_ids: {dataset_ids}")
        if event_id:
            await send_event(event_id, {"task_name": "按关键字获取指标", "task_status": "working"}, "task")
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

                try:
                    # 第一次请求，获取总数
                    first_result = await self._make_async_request(
                        "POST",
                        self.api_paths["get_metric_info"],
                        params={"pi": 1, "ps": page_size},
                        data={"keyword": kw, "datasetIds": dataset_ids, "fuzzyMatch": fuzzy_match}
                    )

                    if str(first_result.get("code", "")) != "0":
                        error_msg = f"获取关键词'{kw}'的第一页失败: {first_result.get('msg', '未知错误')}"
                        logger.warning(error_msg)
                        raise ApiResponseError(error_msg)

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
                            error_msg = f"关键词'{kw}'的异步请求出错: {result!s}"
                            logger.error(error_msg)
                            raise ApiRequestError(error_msg) from result

                        if str(result.get("code", "")) != "0":
                            error_msg = f"关键词'{kw}'的请求返回错误: {result.get('msg', '未知错误')}"
                            logger.warning(error_msg)
                            raise ApiResponseError(error_msg)

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

                except SemanticApiError as e:
                    # 记录错误但继续处理其他关键词
                    logger.error(f"处理关键词 '{kw}' 时出错: {e!s}")
                    raise  # 重新抛出异常

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
            if event_id:
                await send_event(event_id, {"task_name": "按关键字获取指标", "task_data": {
                    "metric_info_list": [
                        {
                            "metric_id": item.get("metricId"),
                            "metric_name": item.get("metricName"),
                            "model_name": item.get("modelName", None)
                        }
                        for item in all_results
                    ]
                }}, "task_data")
                await send_event(event_id, {"task_name": "按关键字获取指标", "task_status": "completed"}, "task")
                await send_event(event_id, {}, "progress_up")
            return all_results

        except Exception as e:
            if not isinstance(e, SemanticApiError):
                error_msg = f"多关键词指标信息搜索过程中出错: {e!s}"
                logger.error(error_msg)
                raise ApiRequestError(error_msg) from e
            raise  # 重新抛出已经包装的异常

    async def get_dimension_info_by_id_async(
            self,
            dimension_ids: str | list[str],
            max_concurrent: int = 5,
            event_id: str | None = None
    ) -> list[dict]:
        """
        异步获取维度详情信息（支持单个ID或ID列表，并发请求）

        Args:
            dimension_ids: 单个维度ID或维度ID列表
            max_concurrent: 最大并发请求数

        Returns:
            List[Dict]: 维度详情信息列表

        Raises:
            ApiRequestError: 请求过程中的错误
            ApiResponseError: API响应错误（业务状态码非0）
        """
        logger.info("\n=== 根据维度ID获取维度详情 ===")
        if event_id:
            await send_event(event_id, {"task_name": "按id获取维度信息", "task_status": "working"}, "task")

        if not dimension_ids:
            return []

        # 确保API路径已添加
        if "get_dimension_info_by_id" not in self.api_paths:
            self.api_paths["get_dimension_info_by_id"] = "/api/drm/semanticOpenApi/getDimensionInfoById"

        # 转换单个ID为列表
        if isinstance(dimension_ids, str):
            dimension_ids = [dimension_ids]

        all_results = []
        error_count = 0  # 跟踪错误数量

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
                        error_msg = f"维度ID {dim_id} 的请求出错: {result!s}"
                        logger.error(error_msg)
                        error_count += 1
                        # 不立即抛出异常，继续处理其他ID，但记录错误
                        continue

                    if str(result.get("code", "")) != "0":
                        error_msg = f"获取维度ID {dim_id} 失败: {result.get('msg', '未知错误')}"
                        logger.warning(error_msg)
                        error_count += 1
                        raise ApiResponseError(error_msg)

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
                        error_msg = f"维度ID {dim_id} 未返回任何详情信息"
                        logger.warning(error_msg)
                        # 空结果也视为一种异常情况
                        raise ApiResponseError(error_msg)

                processed_count += len(batch)
                logger.info(f"已处理 {processed_count}/{len(dimension_ids)} 个维度ID")

            # 全部处理失败的情况
            if error_count == len(dimension_ids):
                raise ApiRequestError(f"所有 {len(dimension_ids)} 个维度ID的请求都失败了")

            # 部分失败的情况下，如果没有任何结果，也抛出异常
            if len(all_results) == 0:
                raise ApiRequestError(f"未能获取到任何维度详情信息，{error_count} 个请求失败")

            # 返回所有结果
            logger.info(f"总共获取到 {len(all_results)} 条维度详情信息")
            if event_id:
                await send_event(event_id, {"task_name": "按id获取维度信息", "task_status": "completed"}, "task")
                await send_event(event_id, {}, "progress_up")
            return all_results

        except Exception as e:
            if isinstance(e, SemanticApiError):
                # 如果是已经封装好的API异常则直接抛出
                raise

            # 其他异常封装为ApiRequestError后抛出
            error_msg = f"获取维度详情过程中出错: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def get_metric_info_by_id_async(
            self,
            metric_ids: str | list[str],
            max_concurrent: int = 5,
            event_id: str | None = None
    ) -> list[dict]:
        """
        异步获取指标详情信息（支持单个ID或ID列表，并发请求）

        Args:
            metric_ids: 单个指标ID或指标ID列表
            max_concurrent: 最大并发请求数

        Returns:
            List[Dict]: 指标详情信息列表

        Raises:
            ApiRequestError: 请求过程中的错误
            ApiResponseError: API响应错误（业务状态码非0）
        """
        logger.info("\n=== 根据指标ID获取指标详情 ===")
        if event_id:
            await send_event(event_id, {"task_name": "按id获取指标", "task_status": "working"}, "task")

        if not metric_ids:
            return []

        # 确保API路径已添加
        if "get_metric_info_by_id" not in self.api_paths:
            self.api_paths["get_metric_info_by_id"] = "/api/drm/semanticOpenApi/getMetricInfoByKId"

        # 转换单个ID为列表
        if isinstance(metric_ids, str):
            metric_ids = [metric_ids]

        all_results = []
        error_count = 0  # 跟踪错误数量

        try:
            # 创建异步任务列表
            tasks = []
            for metric_id in metric_ids:
                logger.info(f"正在准备获取指标ID: {metric_id} 的详情")
                task = self._make_async_request(
                    "POST",
                    self.api_paths["get_metric_info_by_id"],
                    data={"metricId": metric_id}
                )
                tasks.append((metric_id, task))

            # 限制并发请求数
            processed_count = 0
            for i in range(0, len(tasks), max_concurrent):
                batch = tasks[i:i + max_concurrent]
                batch_ids = [metric_id for metric_id, _ in batch]
                batch_tasks = [task for _, task in batch]

                logger.info(
                    f"正在并发获取 {len(batch_ids)} 个指标详情 (ID: {', '.join(batch_ids[:3])}{'...' if len(batch_ids) > 3 else ''})")

                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                # 处理结果
                for idx, (metric_id, result) in enumerate(zip(batch_ids, batch_results)):
                    if isinstance(result, Exception):
                        error_msg = f"指标ID {metric_id} 的请求出错: {result!s}"
                        logger.error(error_msg)
                        error_count += 1
                        # 不立即抛出异常，继续处理其他ID，但记录错误
                        continue

                    if str(result.get("code", "")) != "0":
                        error_msg = f"获取指标ID {metric_id} 失败: {result.get('msg', '未知错误')}"
                        logger.warning(error_msg)
                        error_count += 1
                        raise ApiResponseError(error_msg)

                    # 获取数据部分
                    metric_info = result.get("data", [])

                    if metric_info:
                        # 添加原始请求的指标ID，以便跟踪匹配关系
                        for item in metric_info:
                            if "requested_metric_id" not in item:
                                item["requested_metric_id"] = metric_id

                        all_results.extend(metric_info)
                        logger.info(f"指标ID {metric_id} 返回了 {len(metric_info)} 条详情信息")

                        # 打印第一条详情的关键信息
                        if metric_info:
                            first_info = metric_info[0]
                            metric_name = first_info.get('metricName', 'N/A')
                            metric_en_name = first_info.get('metricEnName', 'N/A')
                            model_name = first_info.get('modelName', 'N/A')
                            logger.info(
                                f"  指标名称={metric_name}, 英文名称={metric_en_name}, 模型名称={model_name}")
                    else:
                        error_msg = f"指标ID {metric_id} 未返回任何详情信息"
                        logger.warning(error_msg)
                        # 空结果也视为一种异常情况
                        raise ApiResponseError(error_msg)

                processed_count += len(batch)
                logger.info(f"已处理 {processed_count}/{len(metric_ids)} 个指标ID")

            # 全部处理失败的情况
            if error_count == len(metric_ids):
                raise ApiRequestError(f"所有 {len(metric_ids)} 个指标ID的请求都失败了")

            # 部分失败的情况下，如果没有任何结果，也抛出异常
            if len(all_results) == 0:
                raise ApiRequestError(f"未能获取到任何指标详情信息，{error_count} 个请求失败")

            # 返回所有结果
            logger.info(f"总共获取到 {len(all_results)} 条指标详情信息")
            if event_id:
                await send_event(event_id, {"task_name": "按id获取指标", "task_data": {
                    "metric_info_list": [
                        {
                            "metric_id": item.get("metricId"),
                            "metric_name": item.get("metricName"),
                            "model_name": item.get("modelName", None)
                        }
                        for item in all_results
                    ]
                }}, "task_data")
                await send_event(event_id, {"task_name": "按id获取指标", "task_status": "completed"}, "task")
                await send_event(event_id, {}, "progress_up")
            return all_results

        except Exception as e:
            if isinstance(e, SemanticApiError):
                # 如果是已经封装好的API异常则直接抛出
                raise

            # 其他异常封装为ApiRequestError后抛出
            error_msg = f"获取指标详情过程中出错: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def get_model_detail_async(
            self,
            model_ids: str | list[str],
            max_concurrent: int = 5
    ) -> list[dict]:
        """
        异步获取模型详情信息（支持单个ID或ID列表，并发请求）

        Args:
            model_ids: 单个模型ID或模型ID列表
            max_concurrent: 最大并发请求数

        Returns:
            List[Dict]: 模型详情信息列表

        Raises:
            ApiRequestError: 请求过程中的错误
            ApiResponseError: API响应错误（业务状态码非0）
            ApiNetworkError: 网络连接错误
        """
        logger.info("\n=== 根据模型ID获取模型详情 ===")

        # 确保API路径已添加
        if "get_model_detail" not in self.api_paths:
            self.api_paths["get_model_detail"] = "/api/drm/semanticOpenApi/getModelDetail"

        # 转换单个ID为列表
        if isinstance(model_ids, str):
            model_ids = [model_ids]

        all_results = []
        error_count = 0  # 跟踪错误数量

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
                        error_msg = f"模型ID {model_id} 的请求出错: {result!s}"
                        logger.error(error_msg)
                        error_count += 1
                        # 不立即抛出异常，继续处理其他ID，但记录错误
                        continue

                    if str(result.get("code", "")) != "0":
                        error_msg = f"获取模型ID {model_id} 失败: {result.get('msg', '未知错误')}"
                        logger.warning(error_msg)
                        error_count += 1
                        raise ApiResponseError(error_msg)

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
                        error_msg = f"模型ID {model_id} 未返回任何详情信息"
                        logger.warning(error_msg)
                        error_count += 1
                        raise ApiResponseError(error_msg)

                processed_count += len(batch)
                logger.info(f"已处理 {processed_count}/{len(model_ids)} 个模型ID")

            # 全部处理失败的情况
            if error_count == len(model_ids):
                raise ApiRequestError(f"所有 {len(model_ids)} 个模型ID的请求都失败了")

            # 部分失败的情况下，如果没有任何结果，也抛出异常
            if len(all_results) == 0:
                raise ApiRequestError(f"未能获取到任何模型详情信息，{error_count}/{len(model_ids)} 个请求失败")

            # 返回所有结果
            logger.info(f"总共获取到 {len(all_results)} 条模型详情信息")
            return all_results

        except SemanticApiError:
            # 如果是已经封装好的API异常则直接抛出
            raise
        except aiohttp.ClientError as e:
            # 网络连接错误单独处理
            error_msg = f"获取模型详情时发生网络错误: {e!s}"
            logger.error(error_msg)
            raise ApiNetworkError(error_msg) from e
        except Exception as e:
            # 其他异常封装为ApiRequestError后抛出
            error_msg = f"获取模型详情过程中出错: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def get_model_relationships_async(
            self,
            model_ids: str | list[str],
            max_concurrent: int = 5
    ) -> list[dict]:
        """
        异步获取模型关系信息（支持单个ID或ID列表，并发请求，根据模型关系去重）

        Args:
            model_ids: 单个模型ID或模型ID列表
            max_concurrent: 最大并发请求数

        Returns:
            List[Dict]: 去重后的模型关系信息列表

        Raises:
            ApiRequestError: 请求过程中的错误
            ApiResponseError: API响应错误（业务状态码非0）
            ApiNetworkError: 网络连接错误
        """
        logger.info("\n=== 根据模型ID获取模型关系 ===")

        # 确保API路径已添加
        if "get_model_relationships" not in self.api_paths:
            self.api_paths["get_model_relationships"] = "/api/drm/semanticOpenApi/getModelRelationships"

        # 转换单个ID为列表
        if isinstance(model_ids, str):
            model_ids = [model_ids]

        all_relationships = []
        relationship_keys = set()  # 用于跟踪已见过的关系组合
        error_count = 0  # 跟踪错误数量
        no_relation_count = 0  # 跟踪无关系的模型数量

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
                        error_msg = f"模型ID {model_id} 的关系请求出错: {result!s}"
                        logger.error(error_msg)
                        error_count += 1
                        # 不立即抛出异常，继续处理其他ID，后续根据错误比例决定
                        continue

                    if str(result.get("code", "")) != "0":
                        error_msg = f"获取模型ID {model_id} 的关系失败: {result.get('msg', '未知错误')}"
                        logger.warning(error_msg)
                        error_count += 1
                        raise ApiResponseError(error_msg)

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
                                error_msg = f"关系数据缺少source或target模型ID: {relation}"
                                logger.warning(error_msg)
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
                            sample_relations = list(all_relationships[-added_count:])[:sample_size]

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
                        # 没有关系不算错误，但记录下来
                        no_relation_count += 1

                processed_count += len(batch)
                logger.info(f"已处理 {processed_count}/{len(model_ids)} 个模型ID")

            # 全部处理失败的情况
            if error_count == len(model_ids):
                raise ApiRequestError(f"所有 {len(model_ids)} 个模型ID的关系请求都失败了")

            # 部分失败的情况下，如果重大失败比例过高，也抛出异常
            if error_count > 0 and error_count / len(model_ids) >= 0.5:  # 50%以上失败率
                raise ApiRequestError(f"超过一半的模型关系请求失败，失败率: {error_count}/{len(model_ids)}")

            # 没有获取到任何关系数据的情况
            if len(all_relationships) == 0:
                # 区分是因为错误还是因为模型确实没有关系
                if error_count > 0:
                    raise ApiRequestError(f"未能获取到任何模型关系信息，{error_count}/{len(model_ids)} 个请求失败")
                elif no_relation_count == len(model_ids):
                    logger.warning(f"所有 {len(model_ids)} 个模型都没有关联关系")
                    # 这种情况下返回空列表，不抛出异常，因为没有关系不算错误

            # 返回所有去重后的结果
            logger.info(f"总共获取到 {len(all_relationships)} 条唯一的模型关系信息")
            return all_relationships

        except SemanticApiError:
            # 如果是已经封装好的API异常则直接抛出
            raise
        except aiohttp.ClientError as e:
            # 网络连接错误单独处理
            error_msg = f"获取模型关系时发生网络错误: {e!s}"
            logger.error(error_msg)
            raise ApiNetworkError(error_msg) from e
        except Exception as e:
            # 其他异常封装为ApiRequestError后抛出
            error_msg = f"获取模型关系过程中出错: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def get_dataset_detail_async(
            self,
            dataset_ids: str | list[str],
            max_concurrent: int = 5,
            event_id: str | None = None
    ) -> list[dict]:
        """
        异步获取数据集详情信息（支持单个ID或ID列表，并发请求）

        Args:
            dataset_ids: 单个数据集ID或数据集ID列表
            max_concurrent: 最大并发请求数

        Returns:
            List[Dict]: 数据集详情信息列表

        Raises:
            ApiRequestError: 请求过程中的错误
            ApiResponseError: API响应业务状态码不为0
            ApiNetworkError: 网络连接错误
        """
        logger.info("\n=== 根据数据集ID获取数据集详情 ===")
        if event_id:
            await send_event(event_id, {"task_name": "获取数据集详情", "task_status": "working"}, "task")

        # 确保API路径已添加
        if "get_dataset_detail" not in self.api_paths:
            self.api_paths["get_dataset_detail"] = "/api/drm/semanticOpenApi/getDatasetDetail"

        # 转换单个ID为列表
        if isinstance(dataset_ids, str):
            dataset_ids = [dataset_ids]

        all_results = []
        error_count = 0  # 跟踪错误数量

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
                        error_msg = f"数据集ID {dataset_id} 的请求出错: {result!s}"
                        logger.error(error_msg)
                        error_count += 1
                        # 记录错误但继续处理其他ID
                        continue

                    if str(result.get("code", "")) != "0":
                        error_msg = f"获取数据集ID {dataset_id} 失败: {result.get('msg', '未知错误')}"
                        logger.warning(error_msg)
                        error_count += 1
                        raise ApiResponseError(error_msg)

                    # 获取数据部分
                    dataset_data = result.get("data")

                    if dataset_data:
                        # 添加原始请求的数据集ID，以便跟踪匹配关系
                        if "requested_dataset_id" not in dataset_data:
                            dataset_data["requested_dataset_id"] = dataset_id

                        # 去除重复的模型（如示例中显示的可能有重复）
                        if dataset_data.get("models"):
                            unique_models = []
                            model_ids_seen = set()
                            for model in dataset_data["models"]:
                                model_id = model.get("modelId")
                                if model_id and model_id not in model_ids_seen:
                                    model_ids_seen.add(model_id)
                                    unique_models.append(model)

                            # 记录是否有重复模型被移除
                            if len(unique_models) < len(dataset_data["models"]):
                                logger.info(
                                    f"数据集ID {dataset_id} 中移除了 {len(dataset_data['models']) - len(unique_models)} 个重复模型")

                            dataset_data["models"] = unique_models

                        # 验证模型列表的完整性
                        if "models" in dataset_data and not dataset_data["models"]:
                            logger.warning(f"数据集ID {dataset_id} 没有关联任何模型")

                        all_results.append(dataset_data)

                        # 打印数据集的关键信息
                        dataset_name = dataset_data.get('datasetName', 'N/A')
                        models_count = len(dataset_data.get('models', []))
                        domain_name = dataset_data.get('domainName', 'N/A')

                        logger.info(
                            f"  已获取数据集: ID={dataset_id}, 名称={dataset_name}, 领域={domain_name}, 关联模型数={models_count}")
                    else:
                        error_msg = f"数据集ID {dataset_id} 未返回任何详情信息"
                        logger.warning(error_msg)
                        error_count += 1
                        raise ApiResponseError(error_msg)

                processed_count += len(batch)
                logger.info(f"已处理 {processed_count}/{len(dataset_ids)} 个数据集ID")

            # 全部处理失败的情况
            if error_count == len(dataset_ids):
                raise ApiRequestError(f"所有 {len(dataset_ids)} 个数据集ID的请求都失败了")

            # 部分失败的情况下，如果失败比例过高，也抛出异常
            if error_count > 0 and error_count / len(dataset_ids) >= 0.5:  # 50%以上失败率
                raise ApiRequestError(f"超过一半的数据集请求失败，失败率: {error_count}/{len(dataset_ids)}")

            # 没有获取到任何结果的情况
            if len(all_results) == 0:
                raise ApiRequestError(f"未能获取到任何数据集详情信息，{error_count}/{len(dataset_ids)} 个请求失败")

            # 返回所有结果
            logger.info(f"总共获取到 {len(all_results)} 条数据集详情信息")

            # 验证结果的完整性
            for result in all_results:
                if "datasetName" not in result or not result["datasetName"]:
                    logger.warning(f"数据集 {result.get('requested_dataset_id', '未知ID')} 缺少必要的名称信息")

                if "models" not in result or not isinstance(result["models"], list):
                    logger.warning(
                        f"数据集 {result.get('datasetName', result.get('requested_dataset_id', '未知ID'))} 的模型信息格式不正确")

            if event_id:
                await send_event(event_id, {"task_name": "获取数据集详情", "task_status": "completed"}, "task")
                await send_event(event_id, {}, "progress_up")

            return all_results

        except SemanticApiError:
            # 如果是已经封装好的API异常则直接抛出
            raise
        except aiohttp.ClientError as e:
            # 网络连接错误单独处理
            error_msg = f"获取数据集详情时发生网络错误: {e!s}"
            logger.error(error_msg)
            raise ApiNetworkError(error_msg) from e
        except Exception as e:
            # 其他异常封装为ApiRequestError后抛出
            error_msg = f"获取数据集详情过程中出错: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def get_business_term_info_async(
            self,
            keyword: str | list[str],
            domain_ids: list[str],
            fuzzy_match: bool = True,
            page_size: int = 100,
            max_pages: int = 10,
            extract_rows: bool = True,
            max_concurrent: int = 5,
            deduplicate_by_term_id: bool = True  # New parameter for deduplication
    ) -> list | list[list]:
        """
        异步获取业务术语信息（支持多关键词，自动分页，并发请求，支持术语ID去重）

        Args:
            keyword: 搜索关键词或关键词列表
            domain_ids: 主题域ID列表
            fuzzy_match: 是否模糊匹配
            page_size: 每页大小
            max_pages: 最大页数限制，防止无限请求
            extract_rows: 是否只提取rows部分数据
            max_concurrent: 最大并发请求数
            deduplicate_by_term_id: 是否根据术语ID去重

        Returns:
            Union[Dict, List[Dict]]: 合并后的完整响应或只包含所有rows的列表（去重后）

        Raises:
            ApiRequestError: 请求过程中的错误
            ApiResponseError: API响应业务状态码不为0
            ApiNetworkError: 网络连接错误
        """
        logger.info("\n=== 根据关键词搜索业务术语 ===")
        logger.info(f"domain_ids: {domain_ids}")

        # 参数验证
        if not domain_ids:
            error_msg = "主题域ID列表不能为空"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        # 转换单个关键词为列表
        if isinstance(keyword, str):
            keywords = [keyword]
        else:
            keywords = keyword

        if not keywords:
            error_msg = "关键词不能为空"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        all_results = []
        term_id_set = set()  # 用于跟踪已见过的termId
        error_count = 0  # 跟踪关键词处理失败的数量
        processed_keywords = 0  # 成功处理的关键词数量

        try:
            # 为每个关键词创建异步任务
            for kw in keywords:
                logger.info(f"正在处理业务术语关键词: {kw}")

                try:
                    # 第一次请求，获取总数
                    first_result = await self._make_async_request(
                        "POST",
                        self.api_paths["get_business_term_info"],
                        params={"pi": 1, "ps": page_size},
                        data={"keyword": kw, "domainIds": domain_ids, "fuzzyMatch": fuzzy_match}
                    )

                    if str(first_result.get("code", "")) != "0":
                        error_msg = f"获取关键词'{kw}'的第一页失败: {first_result.get('msg', '未知错误')}"
                        logger.warning(error_msg)
                        error_count += 1
                        raise ApiResponseError(error_msg)

                    # 计算总页数
                    data = first_result.get("data", {})
                    total = int(data.get("total", 0))
                    total_pages = (total + page_size - 1) // page_size  # 向上取整

                    # 获取第一页结果
                    keyword_results = []
                    duplicate_count = 0
                    if extract_rows:
                        first_page_rows = data.get("rows", [])
                        if first_page_rows:
                            # 处理第一页结果（如果需要去重）
                            for row in first_page_rows:
                                # 给每行添加原始搜索关键词
                                row['search_keyword'] = kw

                                term_id = row.get('termId')
                                if deduplicate_by_term_id:
                                    if term_id and term_id in term_id_set:
                                        duplicate_count += 1
                                        continue
                                    if term_id:
                                        term_id_set.add(term_id)

                                keyword_results.append(row)

                            logger.info(
                                f"关键词'{kw}'的第一页返回了{len(first_page_rows)}条结果，去重后保留{len(first_page_rows) - duplicate_count}条")

                            # 打印样例结果
                            sample_size = min(3, len(keyword_results))
                            for i, row in enumerate(keyword_results[:sample_size]):
                                term_id = row.get('termId', 'N/A')
                                term_name = row.get('termName', 'N/A')
                                term_code = row.get('termCode', 'N/A')
                                logger.info(f"  示例 {i + 1}: ID={term_id}, 术语名称={term_name}, 术语编码={term_code}")

                            if len(keyword_results) > sample_size:
                                logger.info(f"  ...以及其他 {len(keyword_results) - sample_size} 条结果")
                        else:
                            logger.info(f"关键词'{kw}'的第一页没有返回任何结果")
                    else:
                        keyword_results.append(first_result)
                        logger.info(f"关键词'{kw}'的第一页已返回")

                    # 限制最大页数
                    total_pages = min(total_pages, max_pages)

                    if total_pages <= 1:
                        logger.info(f"关键词'{kw}'的总结果数: {len(keyword_results)}条")
                        all_results.extend(keyword_results)
                        processed_keywords += 1
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
                    page_errors = 0
                    for i in range(0, len(page_tasks), max_concurrent):
                        batch = page_tasks[i:i + max_concurrent]
                        logger.info(f"并发获取关键词'{kw}'的 {len(batch)} 个额外页面")
                        batch_results = await asyncio.gather(*batch, return_exceptions=True)

                        for result in batch_results:
                            if isinstance(result, Exception):
                                logger.error(f"获取关键词'{kw}'的页面时发生错误: {result!s}")
                                page_errors += 1
                                continue
                            page_results.append(result)

                    # 处理结果
                    additional_total_rows = 0
                    additional_kept_rows = 0
                    for result in page_results:
                        if str(result.get("code", "")) != "0":
                            error_msg = f"关键词'{kw}'的请求返回错误: {result.get('msg', '未知错误')}"
                            logger.warning(error_msg)
                            page_errors += 1
                            continue

                        if extract_rows:
                            rows = result.get("data", {}).get("rows", [])
                            additional_total_rows += len(rows)

                            # 处理额外页面结果（如果需要去重）
                            for row in rows:
                                # 给每行添加原始搜索关键词
                                row['search_keyword'] = kw

                                term_id = row.get('termId')
                                if deduplicate_by_term_id:
                                    if term_id and term_id in term_id_set:
                                        continue
                                    if term_id:
                                        term_id_set.add(term_id)

                                keyword_results.append(row)
                                additional_kept_rows += 1
                        else:
                            keyword_results.append(result)
                            additional_kept_rows += 1
                            additional_total_rows += 1

                    # 记录页面错误情况
                    if page_errors > 0:
                        logger.warning(f"关键词'{kw}'的 {page_errors}/{len(page_tasks)} 个页面请求失败")

                    logger.info(
                        f"关键词'{kw}'的额外页面返回了{additional_total_rows}条结果，去重后保留{additional_kept_rows}条")
                    logger.info(f"关键词'{kw}'的总结果数: {len(keyword_results)}条")

                    # 添加到总结果
                    all_results.extend(keyword_results)
                    processed_keywords += 1

                except SemanticApiError as e:
                    # 记录错误但继续处理其他关键词
                    logger.error(f"处理关键词 '{kw}' 时出错: {e!s}")
                    error_count += 1
                    # 不抛出异常，继续处理其他关键词

            # 处理结果统计
            logger.info(f"所有业务术语关键词合并后的总结果数: {len(all_results)}条")
            logger.info(f"成功处理了 {processed_keywords}/{len(keywords)} 个关键词")

            # 检查是否所有关键词都处理失败
            if error_count == len(keywords):
                raise ApiRequestError(f"所有 {len(keywords)} 个关键词的业务术语搜索都失败了")

            # 检查是否没有任何结果
            if len(all_results) == 0:
                # 区分是因为错误还是因为确实没有匹配的结果
                if error_count > 0:
                    raise ApiRequestError(f"未能获取到任何业务术语信息，{error_count}/{len(keywords)} 个关键词处理失败")
                else:
                    logger.warning(f"没有找到匹配的业务术语信息，所有 {len(keywords)} 个关键词均无结果")

            # 最后检查一遍确保没有重复的termId（以防万一）
            if deduplicate_by_term_id and extract_rows:
                final_unique_results = []
                final_term_id_set = set()
                for result in all_results:
                    term_id = result.get('termId')
                    if term_id and term_id in final_term_id_set:
                        continue
                    if term_id:
                        final_term_id_set.add(term_id)
                    final_unique_results.append(result)

                # 如果有发现额外的重复项（不应该发生，但以防万一）
                if len(final_unique_results) < len(all_results):
                    logger.warning(
                        f"最终检查时发现额外的重复项: 移除了{len(all_results) - len(final_unique_results)}条重复结果")
                    all_results = final_unique_results

            # 数据完整性验证
            if extract_rows and all_results:
                # 验证结果字段的完整性
                for idx, result in enumerate(all_results):
                    if 'termId' not in result or not result['termId']:
                        logger.warning(f"结果中第 {idx + 1} 条数据缺少术语ID")
                    if 'termName' not in result or not result['termName']:
                        logger.warning(f"术语ID {result.get('termId', '未知')} 缺少术语名称")

            return all_results

        except SemanticApiError:
            # 如果是已经封装好的API异常则直接抛出
            raise
        except aiohttp.ClientError as e:
            # 网络连接错误单独处理
            error_msg = f"业务术语搜索过程中发生网络错误: {e!s}"
            logger.error(error_msg)
            raise ApiNetworkError(error_msg) from e
        except Exception as e:
            # 其他异常封装为ApiRequestError后抛出
            error_msg = f"业务术语搜索过程中出错: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def get_dimension_values_async(
            self,
            dimension_ids: str | list[str],
            page_size: int = 100,
            max_pages: int = 100,
            max_concurrent: int = 5,
            event_id: str | None = None
    ) -> dict[str, list[dict]]:
        """
        异步获取维度值列表（支持单个维度ID或维度ID列表，自动分页，并发请求）

        Args:
            dimension_ids: 单个维度ID或维度ID列表
            page_size: 每页大小
            max_pages: 最大页数限制，防止无限请求
            max_concurrent: 最大并发请求数

        Returns:
            Dict[str, List[Dict]]: 以维度ID为key，维度值列表为value的字典

        Raises:
            ApiRequestError: 请求过程中的错误
            ApiResponseError: API响应业务状态码不为0
            ApiNetworkError: 网络连接错误
        """
        logger.info("\n=== 获取维度值列表 ===")
        if event_id:
            await send_event(event_id, {"task_name": "获取维度值列表", "task_status": "working"}, "task")

        if not dimension_ids:
            return {}

        # 确保API路径已添加
        if "get_dimension_values" not in self.api_paths:
            self.api_paths["get_dimension_values"] = "/api/drm/semanticOpenApi/getDimensionValues"

        # 转换单个ID为列表
        if isinstance(dimension_ids, str):
            dimension_ids = [dimension_ids]

        # 结果字典，key为维度ID，value为维度值列表
        dimension_values_dict = {}
        error_count = 0  # 跟踪错误数量

        try:
            # 为每个维度ID创建并发任务组
            for dimension_id in dimension_ids:
                logger.info(f"正在处理维度ID: {dimension_id}")
                dimension_values = []

                try:
                    # 第一次请求，获取总数和第一页结果
                    first_result = await self._make_async_request(
                        "POST",
                        self.api_paths["get_dimension_values"],
                        params={"pi": 1, "ps": page_size},
                        data={"dimensionId": dimension_id}
                    )

                    if str(first_result.get("code", "")) != "0":
                        error_msg = f"获取维度ID '{dimension_id}' 的第一页值失败: {first_result.get('msg', '未知错误')}"
                        logger.warning(error_msg)
                        error_count += 1
                        raise ApiResponseError(error_msg)

                    # 解析第一页结果
                    data = first_result.get("data", {})
                    total = int(data.get("total", 0))
                    first_page_rows = data.get("rows", [])

                    # 添加第一页结果
                    dimension_values.extend(first_page_rows)

                    logger.info(f"维度ID '{dimension_id}' 第一页返回了 {len(first_page_rows)} 条维度值，总计 {total} 条")

                    # 打印第一页部分示例值
                    if first_page_rows:
                        sample_size = min(3, len(first_page_rows))
                        for i, row in enumerate(first_page_rows[:sample_size]):
                            value = row.get('value', 'N/A')
                            synonyms = row.get('synonyms', [])
                            synonyms_str = ", ".join(synonyms) if synonyms else "无"
                            logger.info(f"  示例 {i + 1}: 值={value}, 同义词={synonyms_str}")

                        if len(first_page_rows) > sample_size:
                            logger.info(f"  ...以及其他 {len(first_page_rows) - sample_size} 条维度值")

                    # 计算总页数
                    total_pages = (total + page_size - 1) // page_size  # 向上取整
                    total_pages = min(total_pages, max_pages)  # 限制最大页数

                    if total_pages <= 1:
                        # 只有一页，直接添加到结果字典
                        dimension_values_dict[dimension_id] = dimension_values
                        logger.info(f"维度ID '{dimension_id}' 只有一页数据，已获取全部 {len(dimension_values)} 条维度值")
                        continue

                    # 创建剩余页面的异步任务
                    page_tasks = []
                    for page in range(2, total_pages + 1):
                        task = self._make_async_request(
                            "POST",
                            self.api_paths["get_dimension_values"],
                            params={"pi": page, "ps": page_size},
                            data={"dimensionId": dimension_id}
                        )
                        page_tasks.append(task)

                    # 限制并发请求数
                    additional_values_count = 0
                    page_errors = 0

                    for i in range(0, len(page_tasks), max_concurrent):
                        batch = page_tasks[i:i + max_concurrent]
                        logger.info(f"并发获取维度ID '{dimension_id}' 的 {len(batch)} 个额外页面")
                        batch_results = await asyncio.gather(*batch, return_exceptions=True)

                        # 处理每个页面的结果
                        for result in batch_results:
                            if isinstance(result, Exception):
                                logger.error(f"获取维度ID '{dimension_id}' 的页面时发生错误: {result!s}")
                                page_errors += 1
                                continue

                            if str(result.get("code", "")) != "0":
                                error_msg = f"维度ID '{dimension_id}' 的请求返回错误: {result.get('msg', '未知错误')}"
                                logger.warning(error_msg)
                                page_errors += 1
                                continue

                            # 提取当前页面的维度值并添加到列表
                            current_page_rows = result.get("data", {}).get("rows", [])
                            dimension_values.extend(current_page_rows)
                            additional_values_count += len(current_page_rows)

                    # 记录页面错误情况
                    if page_errors > 0:
                        logger.warning(f"维度ID '{dimension_id}' 的 {page_errors}/{len(page_tasks)} 个页面请求失败")

                    logger.info(f"维度ID '{dimension_id}' 的额外页面返回了 {additional_values_count} 条维度值")
                    logger.info(f"维度ID '{dimension_id}' 总共获取了 {len(dimension_values)} 条维度值")

                    # 添加到结果字典
                    dimension_values_dict[dimension_id] = dimension_values

                except SemanticApiError as e:
                    # 记录错误但继续处理其他维度ID
                    logger.error(f"处理维度ID '{dimension_id}' 时出错: {e!s}")
                    error_count += 1
                    # 不抛出异常，继续处理其他维度ID

            # 处理结果统计
            successful_dimensions = len(dimension_values_dict)
            logger.info(f"成功获取了 {successful_dimensions}/{len(dimension_ids)} 个维度的值")

            # 检查是否所有维度ID都处理失败
            if error_count == len(dimension_ids):
                raise ApiRequestError(f"所有 {len(dimension_ids)} 个维度ID的值获取都失败了")

            # 检查结果的完整性
            for dim_id, values in dimension_values_dict.items():
                if not values:
                    logger.warning(f"维度ID '{dim_id}' 没有获取到任何维度值")
                else:
                    # 验证值的完整性
                    for idx, value in enumerate(values):
                        if 'value' not in value or not value['value']:
                            logger.warning(f"维度ID '{dim_id}' 的第 {idx + 1} 个维度值缺少value字段")
            if event_id:
                await send_event(event_id, {"task_name": "获取维度值列表", "task_status": "completed"}, "task")
                await send_event(event_id, {}, "progress_up")
            return dimension_values_dict

        except SemanticApiError:
            # 如果是已经封装好的API异常则直接抛出
            raise
        except aiohttp.ClientError as e:
            # 网络连接错误单独处理
            error_msg = f"获取维度值过程中发生网络错误: {e!s}"
            logger.error(error_msg)
            raise ApiNetworkError(error_msg) from e
        except Exception as e:
            # 其他异常封装为ApiRequestError后抛出
            error_msg = f"获取维度值过程中出错: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def get_model_inds_and_dims_by_model_id_async(
            self,
            model_id: str
    ) -> dict | None:
        """
        异步获取单个模型的指标和维度信息。

        Args:
            model_id: 单个模型ID。

        Returns:
            Optional[Dict]: 包含模型指标和维度信息的字典，如果失败则返回 None。

        Raises:
            ApiRequestError: 请求过程中的错误。
            ApiResponseError: API响应业务状态码不为0。
            ApiNetworkError: 网络连接错误。
        """
        logger.info(f"\n=== 根据模型ID {model_id} 获取指标和维度信息 ===")

        if not model_id:
            logger.warning("模型ID不能为空。")
            return None

        try:
            # 直接发起对单个模型ID的请求
            result = await self._make_async_request(
                "POST",
                self.api_paths["get_model_inds_and_dims"],
                data={"modelId": model_id}
            )

            # 获取数据部分
            model_data = result.get("data")

            if model_data and isinstance(model_data, dict):
                # 添加原始请求的模型ID，以便跟踪
                model_data["requested_model_id"] = model_id

                metrics_count = len(model_data.get('metrics', []))
                dims_count = len(model_data.get('dimensions', []))
                logger.info(f"模型ID {model_id} 返回了 {metrics_count} 个指标和 {dims_count} 个维度。")

                return model_data
            else:
                logger.warning(f"模型ID {model_id} 未返回有效数据或数据格式不正确。")
                return None

        except SemanticApiError as e:
            # 捕获已封装的API异常并记录
            logger.error(f"获取模型ID {model_id} 的指标和维度时出错: {e}")
            raise  # 重新抛出，让调用者处理
        except Exception as e:
            # 捕获其他所有异常
            error_msg = f"获取模型 {model_id} 的指标和维度时发生意外错误: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def get_hc_dim_values_by_dim_value_async(
            self,
            keyword: str,
            dimension_id: str,
            user_id: str,
            page_index: int = 1,
            page_size: int = 100,
            fuzzy_match: bool = True
    ) -> dict:
        """
        异步获取高基数维度中的维度值（支持模糊匹配和指定页面）

        Args:
            keyword: 搜索关键词
            dimension_id: 维度ID
            user_id: 用户ID
            page_index: 页码（从1开始）
            page_size: 每页大小
            fuzzy_match: 是否模糊匹配

        Returns:
            Dict: 完整的API响应结果

        Raises:
            ApiRequestError: 请求过程中的错误
            ApiResponseError: API响应业务状态码不为0
            ApiNetworkError: 网络连接错误
        """
        logger.info("\n=== 根据关键词在高基数维度中搜索维度值 ===")
        logger.info(
            f"维度ID: {dimension_id}, 关键词: {keyword}, 用户ID: {user_id}, 页码: {page_index}, 页面大小: {page_size}, 模糊匹配: {fuzzy_match}")

        # 参数验证
        if not keyword:
            error_msg = "搜索关键词不能为空"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        if not dimension_id:
            error_msg = "维度ID不能为空"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        if not user_id:
            error_msg = "用户ID不能为空"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        if page_index < 1:
            error_msg = "页码必须大于等于1"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        if page_size < 1 or page_size > 1000:
            error_msg = "页面大小必须在1-1000之间"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        try:
            # 发起请求，获取指定页面的结果
            logger.info(f"正在获取关键词'{keyword}'在维度'{dimension_id}'中的第{page_index}页结果")
            result = await self._make_async_request(
                "POST",
                self.api_paths["get_hc_dim_values_by_dim_value"],
                params={"pi": page_index, "ps": page_size},
                data={"keyword": keyword, "dimensionId": dimension_id, "fuzzyMatch": fuzzy_match, "userId": user_id}
            )

            if str(result.get("code", "")) != "0":
                error_msg = f"获取第{page_index}页结果失败: {result.get('msg', '未知错误')}"
                logger.warning(error_msg)
                raise ApiResponseError(error_msg)

            # 解析结果
            data_info = result.get("data", {})
            total = int(data_info.get("total", 0))
            page_data = data_info.get("data", [])

            logger.info(f"第{page_index}页返回了 {len(page_data)} 条维度值，总计 {total} 条")

            # 打印部分示例值
            if page_data:
                sample_size = min(5, len(page_data))
                logger.info(f"第{page_index}页示例维度值:")
                for i, item in enumerate(page_data[:sample_size]):
                    value = item.get('mc', 'N/A')
                    logger.info(f"  示例 {i + 1}: {value}")

                if len(page_data) > sample_size:
                    logger.info(f"  ...以及其他 {len(page_data) - sample_size} 条维度值")

            # 数据完整性验证
            if page_data:
                for idx, item in enumerate(page_data):
                    if 'mc' not in item or not item['mc']:
                        logger.warning(f"结果中第 {idx + 1} 条数据缺少mc字段")

            return result

        except SemanticApiError:
            # 如果是已经封装好的API异常则直接抛出
            raise
        except aiohttp.ClientError as e:
            # 网络连接错误单独处理
            error_msg = f"获取高基数维度值过程中发生网络错误: {e!s}"
            logger.error(error_msg)
            raise ApiNetworkError(error_msg) from e
        except Exception as e:
            # 其他异常封装为ApiRequestError后抛出
            error_msg = f"获取高基数维度值过程中出错: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e

    async def get_user_semantic_permissions_async(
            self,
            user_id: str,
            dataset_id_list: list[str] | None = None,
            model_id_list: list[str] | None = None,
            event_id: str | None = None
    ) -> dict | None:
        logger.info("\n=== 获取用户语义权限信息 ===")
        logger.info(f"用户ID: {user_id}")
        if dataset_id_list:
            logger.info(f"数据集ID列表: {dataset_id_list}")
        if model_id_list:
            logger.info(f"模型ID列表: {model_id_list}")
        if event_id:
            await send_event(event_id, {"task_name": "获取用户语义权限信息", "task_status": "working"}, "task")

        # 参数验证
        if not user_id:
            error_msg = "用户ID不能为空"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        # 检查必须提供且只能提供一个参数
        if not dataset_id_list and not model_id_list:
            error_msg = "必须提供 dataset_id_list 或 model_id_list 中的一个"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        if dataset_id_list and model_id_list:
            error_msg = "dataset_id_list 和 model_id_list 不能同时提供，只能选择其一"
            logger.error(error_msg)
            raise ApiRequestError(error_msg)

        try:
            # 根据参数构建请求数据
            request_data = {"userId": user_id}
            if dataset_id_list:
                request_data["dataset_id_list"] = dataset_id_list
            else:  # model_id_list
                request_data["model_id_list"] = model_id_list

            # 发起请求
            result = await self._make_async_request(
                "POST",
                self.api_paths["get_user_semantic_permissions"],
                data=request_data
            )

            # 获取数据部分
            permission_data = result.get("data")

            if permission_data and isinstance(permission_data, dict):
                returned_user_id = permission_data.get('userId', 'N/A')
                data_permissions = permission_data.get('dataPermissions', {})
                # 修复：API返回的是 'models' 而不是 'model'
                models = data_permissions.get('models', [])

                logger.info(f"成功获取用户 {returned_user_id} 的语义权限信息")
                logger.info(f"权限涉及 {len(models)} 个模型")

                # 打印权限详情
                for idx, model_perm in enumerate(models):
                    # 修复：API返回的是 'modelId' 而不是 'model_id'
                    model_id = model_perm.get('modelId', 'N/A')
                    # 修复：API返回的是 'allowedColumns' 而不是 'allowedSemanticField'
                    allowed_fields = model_perm.get('allowedColumns', [])
                    row_filter = model_perm.get('rowFilter', {})

                    logger.info(f"  模型 {idx + 1}: ID={model_id}")
                    logger.info(f"    允许访问的语义字段数量: {len(allowed_fields)}")

                    # 统计维度和指标的数量
                    dim_count = sum(1 for field in allowed_fields if field.get('semanticType') == 'dim')
                    metric_count = sum(1 for field in allowed_fields if field.get('semanticType') == 'metric')
                    logger.info(f"    其中维度: {dim_count} 个, 指标: {metric_count} 个")

                    # 修复：rowFilter 是一个字典对象，需要检查其内容
                    if row_filter and isinstance(row_filter, dict):
                        rules = row_filter.get('rules', [])
                        logical_operator = row_filter.get('logicalOperator', '')
                        if rules:
                            logger.info(f"    行级过滤条件: 逻辑操作符={logical_operator}, 规则数量={len(rules)}")
                        else:
                            logger.info("    行级过滤条件: 已配置但无具体规则")
                    else:
                        logger.info("    无行级过滤条件")

                    # 打印部分字段示例
                    if allowed_fields:
                        sample_size = min(3, len(allowed_fields))
                        logger.info("    字段示例:")
                        for i, field in enumerate(allowed_fields[:sample_size]):
                            semantic_type = field.get('semanticType', 'N/A')
                            semantic_name = field.get('semanticName', 'N/A')
                            semantic_id = field.get('semanticId', 'N/A')
                            # 显示更多字段信息
                            semantic = field.get('semantic', 'N/A')
                            logger.info(
                                f"      {i + 1}. {semantic_type}: {semantic_name} (ID: {semantic_id}, semantic: {semantic})")

                        if len(allowed_fields) > sample_size:
                            logger.info(f"      ...以及其他 {len(allowed_fields) - sample_size} 个字段")

                if event_id:
                    await send_event(event_id, {"task_name": "获取用户语义权限信息", "task_status": "completed"},
                                     "task")
                    await send_event(event_id, {}, "progress_up")

                return permission_data
            else:
                logger.warning(f"用户 {user_id} 未返回有效的权限数据或数据格式不正确")
                return None

        except SemanticApiError as e:
            # 如果是已经封装好的API异常则直接抛出
            logger.error(f"获取用户 {user_id} 的语义权限时出错: {e!s}")
            raise
        except Exception as e:
            # 其他异常封装为ApiRequestError后抛出
            error_msg = f"获取用户语义权限过程中出错: {e!s}"
            logger.error(error_msg)
            raise ApiRequestError(error_msg) from e
