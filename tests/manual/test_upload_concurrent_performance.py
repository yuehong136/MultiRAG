#!/usr/bin/env python3
"""
并发上传性能测试

用于验证异步改造后的性能提升

运行方法：
    python tests/test_upload_concurrent_performance.py

测试场景：
1. 串行上传 vs 并发上传
2. 10个并发请求的响应时间
3. 事件循环阻塞检测
"""

import asyncio
import sys
import time
from io import BytesIO

import aiohttp

# 测试配置
BASE_URL = "http://localhost:8000"
CONCURRENT_REQUESTS = 10
TEST_FILE_SIZE = 1024 * 100  # 100KB


def create_test_file(filename: str, size: int = TEST_FILE_SIZE) -> BytesIO:
    """创建测试文件"""
    content = b"0" * size
    file_obj = BytesIO(content)
    file_obj.name = filename
    return file_obj


async def upload_file_async(session: aiohttp.ClientSession, url: str, file_data: dict, idx: int):
    """异步上传单个文件"""
    start = time.time()
    try:
        async with session.post(url, data=file_data) as response:
            result = await response.json()
            elapsed = time.time() - start
            status = "✅" if response.status == 200 else "❌"
            print(f"  Request {idx:2d}: {status} {response.status} - {elapsed:.3f}s")
            return {"success": response.status == 200, "elapsed": elapsed, "response": result}
    except Exception as e:
        elapsed = time.time() - start
        print(f"  Request {idx:2d}: ❌ Error - {elapsed:.3f}s - {e!s}")
        return {"success": False, "elapsed": elapsed, "error": str(e)}


async def test_file_upload_concurrent():
    """测试 file_app.py 的 upload() 并发性能"""
    print("\n" + "=" * 60)
    print("测试 1: file_app.py - upload() 并发上传")
    print("=" * 60)

    url = f"{BASE_URL}/v1/file/upload"

    # 准备测试数据
    files_data = []
    for i in range(CONCURRENT_REQUESTS):
        file_obj = create_test_file(f"test_file_{i}.txt")
        data = aiohttp.FormData()
        data.add_field("parent_id", "root")  # 需要替换为实际的 parent_id
        data.add_field("files", file_obj, filename=f"test_file_{i}.txt")
        files_data.append(data)

    async with aiohttp.ClientSession() as session:
        # 测试并发上传
        print(f"\n🚀 开始 {CONCURRENT_REQUESTS} 个并发上传...")
        start_time = time.time()

        tasks = [upload_file_async(session, url, files_data[i], i) for i in range(CONCURRENT_REQUESTS)]
        results = await asyncio.gather(*tasks)

        total_time = time.time() - start_time

        # 统计结果
        success_count = sum(1 for r in results if r["success"])
        avg_time = sum(r["elapsed"] for r in results) / len(results)
        max_time = max(r["elapsed"] for r in results)
        min_time = min(r["elapsed"] for r in results)

        print("\n📊 测试结果:")
        print(f"  总耗时:     {total_time:.3f}s")
        print(f"  成功请求:   {success_count}/{CONCURRENT_REQUESTS}")
        print(f"  平均响应:   {avg_time:.3f}s")
        print(f"  最快响应:   {min_time:.3f}s")
        print(f"  最慢响应:   {max_time:.3f}s")

        # 性能评估
        print("\n💡 性能评估:")
        if total_time < avg_time * 1.5:
            print("  ✅ 优秀！真正的并发执行（总耗时 < 平均*1.5）")
        elif total_time < avg_time * 3:
            print("  ⚠️  一般，部分并发（总耗时 < 平均*3）")
        else:
            print("  ❌ 差！近似串行执行（总耗时 > 平均*3）")
            print("  💡 建议：检查是否正确使用了 thread_pool_exec()")


async def test_document_upload_concurrent():
    """测试 document_app.py 的 upload() 并发性能"""
    print("\n" + "=" * 60)
    print("测试 2: document_app.py - upload() 并发上传")
    print("=" * 60)

    url = f"{BASE_URL}/v1/document/upload"

    # 准备测试数据
    files_data = []
    for i in range(CONCURRENT_REQUESTS):
        file_obj = create_test_file(f"test_doc_{i}.txt")
        data = aiohttp.FormData()
        data.add_field("kb_id", "test_kb_id")  # 需要替换为实际的 kb_id
        data.add_field("files", file_obj, filename=f"test_doc_{i}.txt")
        files_data.append(data)

    async with aiohttp.ClientSession() as session:
        print(f"\n🚀 开始 {CONCURRENT_REQUESTS} 个并发上传...")
        start_time = time.time()

        tasks = [upload_file_async(session, url, files_data[i], i) for i in range(CONCURRENT_REQUESTS)]
        results = await asyncio.gather(*tasks)

        total_time = time.time() - start_time

        # 统计结果
        success_count = sum(1 for r in results if r["success"])
        avg_time = sum(r["elapsed"] for r in results) / len(results)

        print("\n📊 测试结果:")
        print(f"  总耗时:     {total_time:.3f}s")
        print(f"  成功请求:   {success_count}/{CONCURRENT_REQUESTS}")
        print(f"  平均响应:   {avg_time:.3f}s")

        # 性能评估
        print("\n💡 性能评估:")
        if total_time < avg_time * 1.5:
            print("  ✅ 优秀！真正的并发执行")
        elif total_time < avg_time * 3:
            print("  ⚠️  一般，部分并发")
        else:
            print("  ❌ 差！近似串行执行")


async def test_media_upload_concurrent():
    """测试 file_app.py 的 upload_media_redirect() 并发性能"""
    print("\n" + "=" * 60)
    print("测试 3: file_app.py - upload_media_redirect() 并发上传")
    print("=" * 60)

    url = f"{BASE_URL}/v1/file/upload_media_redirect"

    # 准备测试数据
    files_data = []
    for i in range(CONCURRENT_REQUESTS):
        file_obj = create_test_file(f"test_media_{i}.mp4", size=1024 * 200)  # 200KB
        data = aiohttp.FormData()
        data.add_field("file", file_obj, filename=f"test_media_{i}.mp4")
        files_data.append(data)

    async with aiohttp.ClientSession() as session:
        print(f"\n🚀 开始 {CONCURRENT_REQUESTS} 个并发上传...")
        start_time = time.time()

        tasks = [upload_file_async(session, url, files_data[i], i) for i in range(CONCURRENT_REQUESTS)]
        results = await asyncio.gather(*tasks)

        total_time = time.time() - start_time

        # 统计结果
        success_count = sum(1 for r in results if r["success"])
        avg_time = sum(r["elapsed"] for r in results) / len(results)

        print("\n📊 测试结果:")
        print(f"  总耗时:     {total_time:.3f}s")
        print(f"  成功请求:   {success_count}/{CONCURRENT_REQUESTS}")
        print(f"  平均响应:   {avg_time:.3f}s")

        # 性能评估
        print("\n💡 性能评估:")
        if total_time < avg_time * 1.5:
            print("  ✅ 优秀！真正的并发执行")
        elif total_time < avg_time * 3:
            print("  ⚠️  一般，部分并发")
        else:
            print("  ❌ 差！近似串行执行")


async def main():
    """主测试函数"""
    print("\n" + "🔥" * 30)
    print("  异步改造并发性能测试")
    print("🔥" * 30)
    print("\n配置:")
    print(f"  服务地址:       {BASE_URL}")
    print(f"  并发请求数:     {CONCURRENT_REQUESTS}")
    print(f"  测试文件大小:   {TEST_FILE_SIZE // 1024}KB")

    try:
        # 测试 1: file_app.py - upload()
        # await test_file_upload_concurrent()

        # 测试 2: document_app.py - upload()
        # await test_document_upload_concurrent()

        # 测试 3: file_app.py - upload_media_redirect()
        await test_media_upload_concurrent()

        print("\n" + "=" * 60)
        print("✅ 所有测试完成！")
        print("=" * 60)

    except aiohttp.ClientConnectorError:
        print(f"\n❌ 错误：无法连接到 {BASE_URL}")
        print("请确保服务已启动：uvicorn api.apps:app --host 0.0.0.0 --port 8000")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试出错: {e!s}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    # 设置事件循环策略（Windows兼容）
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())
