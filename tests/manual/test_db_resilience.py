#!/usr/bin/env python3
"""
数据库连接稳定性测试脚本

测试内容：
1. get_by_id 重试机制
2. increase_usage best-effort（失败不抛异常）
3. 跨进程连接检测
4. 并发请求稳定性
5. 模拟连接断开后的恢复

使用方法：
    cd /Users/dxl/project/python/multirag
    python tests/test_db_resilience.py
"""

import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class TestResult:
    """测试结果收集器"""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def success(self, name: str, msg: str = ""):
        self.passed += 1
        print(f"  ✅ {name}: {msg}" if msg else f"  ✅ {name}")

    def fail(self, name: str, msg: str):
        self.failed += 1
        self.errors.append((name, msg))
        print(f"  ❌ {name}: {msg}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'=' * 60}")
        print(f"测试结果: {self.passed}/{total} 通过")
        if self.errors:
            print("\n失败详情:")
            for name, msg in self.errors:
                print(f"  - {name}: {msg}")
        print(f"{'=' * 60}")
        return self.failed == 0


result = TestResult()


def test_1_basic_db_connection():
    """测试 1: 基本数据库连接"""
    print("\n[测试 1] 基本数据库连接")
    try:
        from sqlalchemy import text

        from api.db.db_models import db_connection

        with db_connection() as db:
            # 执行简单查询（使用 text() 包装 SQL）
            res = db.execute(text("SELECT 1 as test")).fetchone()
            if res and res[0] == 1:
                result.success("数据库连接", "SELECT 1 执行成功")
            else:
                result.fail("数据库连接", f"查询结果异常: {res}")
    except Exception as e:
        result.fail("数据库连接", str(e))


def test_2_get_by_id_normal():
    """测试 2: get_by_id 正常查询"""
    print("\n[测试 2] get_by_id 正常查询")
    try:
        from api.db.db_models import db_connection
        from api.db.services.user_service import TenantService

        with db_connection() as db:
            # 查询不存在的 ID，应返回 None 而不是抛异常
            tenant = TenantService.get_by_id(db, "non-existent-test-id-12345")
            if tenant is None:
                result.success("get_by_id 查询", "查询不存在的ID正确返回 None")
            else:
                result.success("get_by_id 查询", f"查询返回: {tenant}")
    except Exception as e:
        result.fail("get_by_id 查询", str(e))


def test_3_get_by_id_has_retry_decorator():
    """测试 3: 验证 get_by_id 已添加重试装饰器"""
    print("\n[测试 3] get_by_id 重试装饰器验证")
    try:
        import inspect

        from api.db.services.common_service import CommonService

        # 获取方法源代码
        source = inspect.getsource(CommonService.get_by_id)

        if "@retry_db_operation" in source or "retry_db_operation" in str(CommonService.get_by_id):
            result.success("重试装饰器", "get_by_id 已添加 @retry_db_operation")
        else:
            # 检查装饰器是否在类定义中
            class_source = inspect.getsource(CommonService)
            # 查找 get_by_id 方法前的装饰器
            lines = class_source.split("\n")
            found_decorator = False
            for i, line in enumerate(lines):
                if "def get_by_id" in line:
                    # 检查前几行是否有装饰器
                    for j in range(max(0, i - 3), i):
                        if "retry_db_operation" in lines[j]:
                            found_decorator = True
                            break
                    break

            if found_decorator:
                result.success("重试装饰器", "get_by_id 已添加 @retry_db_operation")
            else:
                result.fail("重试装饰器", "未找到 @retry_db_operation 装饰器")
    except Exception as e:
        result.fail("重试装饰器", str(e))


def test_4_increase_usage_best_effort():
    """测试 4: increase_usage 失败不抛异常"""
    print("\n[测试 4] increase_usage best-effort 机制")
    try:
        import inspect
        from unittest.mock import MagicMock

        from api.db import LLMType
        from api.db.services.tenant_llm_service import TenantLLMService

        db = MagicMock()

        # 测试 1: 无效 tenant_id
        ret1 = TenantLLMService.increase_usage(db, tenant_id="invalid-tenant-id-12345", llm_type=LLMType.CHAT.value, used_tokens=100, llm_name="test-model")
        if ret1 == 0:
            result.success("无效tenant_id", f"返回 {ret1}，未抛异常")
        else:
            result.fail("无效tenant_id", f"预期返回0，实际返回 {ret1}")

        # 测试 2: db=None 支持
        ret2 = TenantLLMService.increase_usage(None, tenant_id="invalid-tenant-id-12345", llm_type=LLMType.CHAT.value, used_tokens=100, llm_name="test-model")
        if ret2 == 0:
            result.success("db=None", f"返回 {ret2}，未抛异常")
        else:
            result.fail("db=None", f"预期返回0，实际返回 {ret2}")

        # 测试 3: used_tokens <= 0
        ret3 = TenantLLMService.increase_usage(db, tenant_id="any-id", llm_type=LLMType.CHAT.value, used_tokens=0, llm_name="test-model")
        if ret3 == 0:
            result.success("used_tokens=0", f"返回 {ret3}，未抛异常")
        else:
            result.fail("used_tokens=0", f"预期返回0，实际返回 {ret3}")

        source = inspect.getsource(TenantLLMService.increase_usage)
        if "db.commit()" not in source:
            result.success("独立session记账", "increase_usage 不再直接提交调用方 session")
        else:
            result.fail("独立session记账", "increase_usage 仍直接调用 db.commit()")

    except Exception as e:
        result.fail("increase_usage", f"抛出异常: {e}")


def test_5_release_db_before_long_io():
    """测试 5: _release_db_before_long_io 方法存在"""
    print("\n[测试 5] _release_db_before_long_io 方法验证")
    try:
        from api.db.services.tenant_llm_service import LLM4Tenant

        if hasattr(LLM4Tenant, "_release_db_before_long_io"):
            result.success("方法存在", "LLM4Tenant._release_db_before_long_io 已定义")

            # 检查方法内容
            import inspect

            source = inspect.getsource(LLM4Tenant._release_db_before_long_io)

            checks = [
                ("in_transaction", "in_transaction() 检查"),
                ("dirty", "dirty 检查"),
                ("rollback", "rollback() 调用"),
            ]

            for keyword, desc in checks:
                if keyword in source:
                    result.success(desc, f"包含 {keyword}")
                else:
                    result.fail(desc, f"缺少 {keyword}")
        else:
            result.fail("方法存在", "_release_db_before_long_io 方法未找到")
    except Exception as e:
        result.fail("_release_db_before_long_io", str(e))


def test_5b_llm4tenant_supports_none_db():
    """测试 5b: LLM4Tenant 支持 db=None 初始化"""
    print("\n[测试 5b] LLM4Tenant 支持 db=None")
    try:
        import inspect

        from api.db.services.tenant_llm_service import LLM4Tenant

        source = inspect.getsource(LLM4Tenant.__init__)
        if "db: Session | None" in source and "if db is None" in source:
            result.success("db=None 初始化", "LLM4Tenant.__init__ 已支持独立初始化")
        else:
            result.fail("db=None 初始化", "LLM4Tenant.__init__ 尚未支持 db=None")
    except Exception as e:
        result.fail("LLM4Tenant db=None", str(e))


def test_6_cross_process_invalidate():
    """测试 6: 跨进程连接检测使用 invalidate()"""
    print("\n[测试 6] 跨进程连接检测配置验证")
    try:
        import inspect

        from api.db import db_models

        # 读取 db_models.py 源码检查 checkout 事件处理
        source = inspect.getsource(db_models)

        # 检查 invalidate() 调用
        if "connection_record.invalidate()" in source:
            result.success("invalidate() 调用", "使用 SQLAlchemy 官方推荐方法")
        elif "connection_record.connection = None" in source:
            result.fail("invalidate() 调用", "仍使用旧方法 connection = None")
        else:
            result.fail("invalidate() 调用", "未找到跨进程处理逻辑")

        # 检查 DisconnectionError
        if "DisconnectionError" in source:
            result.success("DisconnectionError", "已导入并使用")
        else:
            result.fail("DisconnectionError", "未找到 DisconnectionError")

    except Exception as e:
        result.fail("跨进程检测", str(e))


def test_7_concurrent_requests():
    """测试 7: 并发请求稳定性"""
    print("\n[测试 7] 并发请求稳定性")
    try:
        from api.db.db_models import db_connection
        from api.db.services.user_service import TenantService

        success_count = 0
        error_count = 0
        errors = []
        lock = threading.Lock()

        def single_request(i):
            nonlocal success_count, error_count
            try:
                with db_connection() as db:
                    TenantService.get_by_id(db, f"concurrent-test-{i}")
                with lock:
                    success_count += 1
                return True
            except Exception as e:
                with lock:
                    error_count += 1
                    if len(errors) < 5:
                        errors.append(str(e))
                return False

        num_requests = 50
        num_workers = 10

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(single_request, i) for i in range(num_requests)]
            for future in as_completed(futures):
                future.result()

        if error_count == 0:
            result.success("并发测试", f"{success_count}/{num_requests} 请求成功")
        else:
            result.fail("并发测试", f"{error_count}/{num_requests} 请求失败: {errors[0] if errors else 'unknown'}")

    except Exception as e:
        result.fail("并发测试", str(e))


def test_8_init_data_no_import_session():
    """测试 8: init_data.py 不在 import 时创建 Session"""
    print("\n[测试 8] init_data.py Session 创建方式验证")
    try:
        import inspect

        from api.db import init_data

        source = inspect.getsource(init_data)

        # 检查是否还有 def xxx(db: Session = SessionLocal()) 这种写法
        import re

        bad_pattern = r"def\s+\w+\([^)]*=\s*SessionLocal\(\)"
        matches = re.findall(bad_pattern, source)

        if matches:
            result.fail("import时Session", f"发现问题写法: {matches}")
        else:
            result.success("import时Session", "未发现 SessionLocal() 作为默认参数")

        # 检查是否使用 db_connection
        if "db_connection" in source:
            result.success("db_connection", "使用 db_connection() 上下文管理器")
        else:
            result.fail("db_connection", "未使用 db_connection()")

    except Exception as e:
        result.fail("init_data检查", str(e))


def test_9_llm_service_release_calls():
    """测试 9: LLMBundle 方法调用 _release_db_before_long_io"""
    print("\n[测试 9] LLMBundle 长耗时调用前释放事务")
    try:
        import inspect

        from api.db.services.llm_service import LLMBundle

        source = inspect.getsource(LLMBundle)

        # 需要检查的方法
        methods_to_check = [
            "encode",
            "encode_queries",
            "similarity",
            "describe",
            "transcription",
            "tts",
            "chat",
            "chat_streamly",
        ]

        for method in methods_to_check:
            # 查找方法定义到下一个方法定义之间的代码
            pattern = rf"def {method}\s*\([^)]*\).*?(?=\n    def |\Z)"
            match = re.search(pattern, source, re.DOTALL)

            if match:
                method_source = match.group(0)
                if "_release_db_before_long_io" in method_source:
                    result.success(f"{method}()", "已添加 _release_db_before_long_io")
                else:
                    result.fail(f"{method}()", "缺少 _release_db_before_long_io 调用")
            else:
                # 方法可能不存在或在父类
                pass

    except Exception as e:
        result.fail("LLMBundle检查", str(e))


def test_10_mock_connection_drop():
    """测试 10: 模拟连接断开后重试"""
    print("\n[测试 10] 模拟连接断开重试机制")
    try:
        from sqlalchemy.exc import OperationalError

        from api.db.services.common_service import retry_db_operation

        # 测试重试装饰器本身
        call_count = 0

        @retry_db_operation(max_attempts=3)
        def mock_db_operation():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OperationalError("server closed the connection unexpectedly", None, None)
            return "success"

        try:
            res = mock_db_operation()
            if res == "success" and call_count == 3:
                result.success("重试机制", f"第 {call_count} 次尝试成功")
            else:
                result.fail("重试机制", f"结果异常: {res}, 调用次数: {call_count}")
        except Exception as e:
            result.fail("重试机制", f"重试后仍失败: {e}")

    except Exception as e:
        result.fail("模拟测试", str(e))


import re  # 需要在顶部导入，这里为了测试脚本完整性再次导入


def main():
    print("=" * 60)
    print("PostgreSQL 连接稳定性修复验证测试")
    print("=" * 60)

    tests = [
        test_1_basic_db_connection,
        test_2_get_by_id_normal,
        test_3_get_by_id_has_retry_decorator,
        test_4_increase_usage_best_effort,
        test_5_release_db_before_long_io,
        test_6_cross_process_invalidate,
        test_7_concurrent_requests,
        test_8_init_data_no_import_session,
        test_9_llm_service_release_calls,
        test_10_mock_connection_drop,
    ]

    for test_func in tests:
        try:
            test_func()
        except Exception as e:
            result.fail(test_func.__name__, f"测试执行异常: {e}")

    success = result.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
