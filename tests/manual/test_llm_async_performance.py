#!/usr/bin/env python3
"""
LLM 接口异步性能测试脚本

测试目标：
1. 验证接口是否会阻塞
2. 测试并发性能
3. 对比流式/非流式响应时间
4. 测试不同并发数下的表现

使用方法：
    python tests/test_llm_async_performance.py
"""

import asyncio
import json
import os
import statistics
import time
from dataclasses import dataclass

import aiohttp

# ============ 配置区域 ============
BASE_URL = os.environ.get("MULTIRAG_PERF_BASE_URL", "http://127.0.0.1:8123")
# 会话 JWT 不入库：运行前 export MULTIRAG_PERF_TOKEN="Bearer eyJ..."
TOKEN = os.environ.get("MULTIRAG_PERF_TOKEN", "")
LLM_NAME = "glm-4-airx"

# 使用复杂的问题确保 LLM 需要真正思考并生成较长回答
COMPLEX_PROMPTS = [
    "请详细解释什么是机器学习中的梯度下降算法，包括其数学原理和应用场景，至少200字",
    "请用500字左右介绍Python异步编程的原理，包括协程、事件循环、async/await的工作机制",
    "请详细分析微服务架构的优缺点，以及在什么场景下应该使用微服务，至少300字",
    "请解释数据库索引的工作原理，B+树索引和哈希索引的区别，以及如何优化查询性能",
    "请详细介绍Docker容器技术的原理，与虚拟机的区别，以及在生产环境中的最佳实践",
    "请解释分布式系统中的CAP定理，以及如何在一致性和可用性之间做权衡",
    "请介绍深度学习中Transformer架构的原理，包括自注意力机制的工作方式",
    "请解释什么是RESTful API设计原则，以及如何设计一个好的API接口",
    "请介绍消息队列的使用场景，比较Kafka和RabbitMQ的区别",
    "请解释什么是函数式编程，它与面向对象编程有什么区别，各自的优缺点是什么",
]

HEADERS = {
    "Authorization": TOKEN,
    "Content-Type": "application/json",
    "Accept": "*/*",
}


# ============ 数据类 ============
@dataclass
class RequestResult:
    """单次请求结果"""

    request_id: int
    success: bool
    start_time: float
    end_time: float
    first_byte_time: float | None  # 首字节时间（流式）
    status_code: int
    error: str | None
    full_response: str  # 完整响应
    response_length: int  # 响应字符数

    @property
    def total_time(self) -> float:
        return self.end_time - self.start_time

    @property
    def ttfb(self) -> float | None:
        """Time To First Byte"""
        if self.first_byte_time:
            return self.first_byte_time - self.start_time
        return None


# ============ 测试函数 ============
async def test_single_request(session: aiohttp.ClientSession, request_id: int, stream: bool = True, prompt: str = None) -> RequestResult:
    """发送单个请求并记录时间"""

    if prompt is None:
        prompt = COMPLEX_PROMPTS[request_id % len(COMPLEX_PROMPTS)]

    url = f"{BASE_URL}/v1/llm/chat_service_sse"
    payload = {
        "prompt": "",
        "messages": [{"role": "user", "content": prompt}],
        "llm_name": LLM_NAME,
        "stream": stream,
        "gen_conf": {"max_tokens": 1000},  # 允许更长的回答
    }

    start_time = time.time()
    first_byte_time = None
    full_response = ""
    status_code = 0
    error = None

    try:
        async with session.post(url, json=payload, headers=HEADERS) as response:
            status_code = response.status

            if stream:
                # 流式响应 - 收集完整内容
                async for line in response.content:
                    if first_byte_time is None:
                        first_byte_time = time.time()

                    line_str = line.decode("utf-8").strip()
                    if line_str.startswith("data: "):
                        data_str = line_str[6:]
                        try:
                            data = json.loads(data_str)
                            if isinstance(data.get("data"), str):
                                full_response = data["data"]  # 累积响应
                            elif data.get("data") == True:
                                pass  # 结束标记
                        except json.JSONDecodeError:
                            pass
            else:
                # 非流式响应
                first_byte_time = time.time()
                resp_text = await response.text()
                try:
                    data = json.loads(resp_text)
                    full_response = str(data.get("data", ""))
                except json.JSONDecodeError:
                    full_response = resp_text

    except Exception as e:
        error = str(e)

    end_time = time.time()

    return RequestResult(
        request_id=request_id,
        success=status_code == 200 and error is None,
        start_time=start_time,
        end_time=end_time,
        first_byte_time=first_byte_time,
        status_code=status_code,
        error=error,
        full_response=full_response,
        response_length=len(full_response),
    )


async def run_concurrent_test(num_requests: int, stream: bool = True, use_same_prompt: bool = False) -> list[RequestResult]:
    """并发运行多个请求"""

    connector = aiohttp.TCPConnector(limit=num_requests + 10)
    timeout = aiohttp.ClientTimeout(total=180)  # 增加超时时间

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        if use_same_prompt:
            prompt = COMPLEX_PROMPTS[0]
            tasks = [test_single_request(session, i, stream, prompt) for i in range(num_requests)]
        else:
            tasks = [test_single_request(session, i, stream, None) for i in range(num_requests)]

        print(f"\n🚀 同时发送 {num_requests} 个{'流式' if stream else '非流式'}请求...")
        batch_start = time.time()

        results = await asyncio.gather(*tasks)

        batch_end = time.time()
        print(f"✅ 所有请求完成，总耗时: {batch_end - batch_start:.2f}s")

        return results


def analyze_results(results: list[RequestResult], test_name: str, show_full_response: bool = False):
    """分析测试结果"""

    print(f"\n{'=' * 60}")
    print(f"📊 {test_name} 测试结果分析")
    print(f"{'=' * 60}")

    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    print("\n📈 基本统计:")
    print(f"   总请求数: {len(results)}")
    print(f"   成功: {len(successful)} ({len(successful) / len(results) * 100:.1f}%)")
    print(f"   失败: {len(failed)} ({len(failed) / len(results) * 100:.1f}%)")

    if failed:
        print("\n❌ 失败详情:")
        for r in failed[:5]:
            print(f"   请求 {r.request_id}: 状态码={r.status_code}, 错误={r.error}")

    if successful:
        times = [r.total_time for r in successful]
        lengths = [r.response_length for r in successful]

        print("\n⏱️ 响应时间统计:")
        print(f"   最小: {min(times):.3f}s")
        print(f"   最大: {max(times):.3f}s")
        print(f"   平均: {statistics.mean(times):.3f}s")
        print(f"   中位数: {statistics.median(times):.3f}s")
        if len(times) > 1:
            print(f"   标准差: {statistics.stdev(times):.3f}s")

        print("\n📝 响应长度统计:")
        print(f"   最短: {min(lengths)} 字符")
        print(f"   最长: {max(lengths)} 字符")
        print(f"   平均: {statistics.mean(lengths):.0f} 字符")

        # 首字节时间（流式）
        ttfb_list = [r.ttfb for r in successful if r.ttfb is not None]
        if ttfb_list:
            print("\n🚀 首字节时间 (TTFB):")
            print(f"   最小: {min(ttfb_list):.3f}s")
            print(f"   最大: {max(ttfb_list):.3f}s")
            print(f"   平均: {statistics.mean(ttfb_list):.3f}s")

        # 阻塞分析
        print("\n🔍 阻塞分析:")
        start_times = sorted([r.start_time for r in results])
        end_times = sorted([r.end_time for r in results])

        start_spread = start_times[-1] - start_times[0]
        print(f"   请求开始时间差: {start_spread:.3f}s")

        end_spread = end_times[-1] - end_times[0]
        print(f"   请求结束时间差: {end_spread:.3f}s")

        avg_time = statistics.mean(times)
        if end_spread > avg_time * 1.5 and len(results) > 1:
            print("   ⚠️  可能存在阻塞: 结束时间差异较大")
        else:
            print("   ✅ 无明显阻塞: 请求几乎并行完成")

        # 吞吐量
        total_duration = max(end_times) - min(start_times)
        throughput = len(successful) / total_duration if total_duration > 0 else 0
        print(f"\n📊 吞吐量: {throughput:.2f} 请求/秒")

        # 显示响应内容
        if show_full_response:
            print("\n💬 各请求响应内容:")
            for r in successful:
                print(f"\n--- 请求 {r.request_id} ({r.response_length} 字符, {r.total_time:.2f}s) ---")
                # 显示前500字符
                preview = r.full_response[:500]
                if len(r.full_response) > 500:
                    preview += "..."
                print(preview)
        else:
            # 只显示第一个响应的预览
            if successful:
                r = successful[0]
                print(f"\n💬 响应预览 (请求 0, 共 {r.response_length} 字符):")
                preview = r.full_response[:300]
                if len(r.full_response) > 300:
                    preview += "..."
                print(f"   {preview}")


async def test_blocking_detection():
    """阻塞检测测试"""
    print("\n" + "=" * 60)
    print("🔬 阻塞检测测试 (复杂问题)")
    print("=" * 60)
    print("使用复杂问题确保 LLM 需要真正思考和生成长回答")
    print("原理: 发送多个并发请求，观察完成时间分布")
    print("      如果阻塞 → 请求串行完成（阶梯状）")
    print("      如果不阻塞 → 请求并行完成（同时结束）")

    results = await run_concurrent_test(5, stream=True, use_same_prompt=False)

    # 按完成时间排序
    sorted_results = sorted(results, key=lambda r: r.end_time)

    print("\n📊 请求完成时间分布:")
    base_time = sorted_results[0].start_time
    for r in sorted_results:
        rel_start = r.start_time - base_time
        rel_end = r.end_time - base_time
        bar_length = int(r.total_time * 5)  # 调整比例
        bar = "█" * bar_length
        print(f"   请求 {r.request_id}: [{rel_start:.2f}s] {bar} [{rel_end:.2f}s] ({r.total_time:.2f}s, {r.response_length}字符)")

    analyze_results(results, "阻塞检测", show_full_response=True)


async def test_concurrent_scaling():
    """并发扩展性测试"""
    print("\n" + "=" * 60)
    print("📈 并发扩展性测试")
    print("=" * 60)

    concurrency_levels = [1, 3, 5, 10]
    all_results = {}

    for num in concurrency_levels:
        print(f"\n--- 测试 {num} 并发 ---")
        results = await run_concurrent_test(num, stream=True, use_same_prompt=True)
        all_results[num] = results

        successful = [r for r in results if r.success]
        if successful:
            avg_time = statistics.mean([r.total_time for r in successful])
            avg_len = statistics.mean([r.response_length for r in successful])
            print(f"   平均响应时间: {avg_time:.2f}s, 平均响应长度: {avg_len:.0f}字符")

        await asyncio.sleep(2)  # 增加间隔避免限流

    # 汇总分析
    print(f"\n{'=' * 60}")
    print("📊 并发扩展性汇总")
    print(f"{'=' * 60}")
    print(f"{'并发数':<8} {'成功率':<10} {'平均时间':<12} {'平均长度':<12} {'吞吐量':<12}")
    print("-" * 54)

    for num, results in all_results.items():
        successful = [r for r in results if r.success]
        success_rate = len(successful) / len(results) * 100

        if successful:
            avg_time = statistics.mean([r.total_time for r in successful])
            avg_len = statistics.mean([r.response_length for r in successful])
            total_duration = max(r.end_time for r in results) - min(r.start_time for r in results)
            throughput = len(successful) / total_duration if total_duration > 0 else 0
            print(f"{num:<8} {success_rate:<10.1f}% {avg_time:<12.2f}s {avg_len:<12.0f} {throughput:<12.2f}req/s")
        else:
            print(f"{num:<8} {success_rate:<10.1f}% {'N/A':<12} {'N/A':<12} {'N/A':<12}")


async def test_stream_vs_non_stream():
    """流式 vs 非流式对比测试"""
    print("\n" + "=" * 60)
    print("🔄 流式 vs 非流式对比测试")
    print("=" * 60)

    num_requests = 3

    # 流式测试
    print("\n--- 流式响应测试 ---")
    stream_results = await run_concurrent_test(num_requests, stream=True, use_same_prompt=True)

    await asyncio.sleep(3)

    # 非流式测试
    print("\n--- 非流式响应测试 ---")
    non_stream_results = await run_concurrent_test(num_requests, stream=False, use_same_prompt=True)

    # 对比分析
    print(f"\n{'=' * 60}")
    print("📊 流式 vs 非流式对比")
    print(f"{'=' * 60}")

    for name, results in [("流式", stream_results), ("非流式", non_stream_results)]:
        successful = [r for r in results if r.success]
        if successful:
            avg_time = statistics.mean([r.total_time for r in successful])
            avg_len = statistics.mean([r.response_length for r in successful])
            ttfb_list = [r.ttfb for r in successful if r.ttfb]
            avg_ttfb = statistics.mean(ttfb_list) if ttfb_list else None

            print(f"\n{name}:")
            print(f"   平均总时间: {avg_time:.3f}s")
            print(f"   平均响应长度: {avg_len:.0f} 字符")
            if avg_ttfb:
                print(f"   平均首字节: {avg_ttfb:.3f}s")


async def quick_health_check():
    """快速健康检查 - 使用复杂问题"""
    print("\n" + "=" * 60)
    print("🏥 快速健康检查 (复杂问题验证)")
    print("=" * 60)

    connector = aiohttp.TCPConnector()
    timeout = aiohttp.ClientTimeout(total=60)

    prompt = "请用200字左右解释什么是Python的GIL（全局解释器锁），它对多线程有什么影响？"
    print(f"测试问题: {prompt}")

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        result = await test_single_request(session, 0, stream=True, prompt=prompt)

        if result.success:
            print("\n✅ 接口正常")
            print(f"   响应时间: {result.total_time:.2f}s")
            print(f"   首字节时间: {result.ttfb:.2f}s" if result.ttfb else "   首字节时间: N/A")
            print(f"   响应长度: {result.response_length} 字符")
            print("\n📝 完整响应内容:")
            print("-" * 40)
            print(result.full_response)
            print("-" * 40)
            return True
        else:
            print("❌ 接口异常")
            print(f"   状态码: {result.status_code}")
            print(f"   错误: {result.error}")
            return False


async def main():
    """主测试流程"""
    print("=" * 60)
    print("🧪 LLM 异步接口性能测试 (完整验证版)")
    print("=" * 60)
    print(f"目标: {BASE_URL}")
    print(f"模型: {LLM_NAME}")
    print("说明: 使用复杂问题确保 LLM 真正工作并生成长回答")

    # 1. 健康检查
    if not await quick_health_check():
        print("\n⚠️  健康检查失败，请检查服务状态")
        return

    # 2. 阻塞检测
    await test_blocking_detection()

    # 3. 流式 vs 非流式
    await test_stream_vs_non_stream()

    # 4. 并发扩展性测试
    await test_concurrent_scaling()

    print("\n" + "=" * 60)
    print("✅ 所有测试完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
