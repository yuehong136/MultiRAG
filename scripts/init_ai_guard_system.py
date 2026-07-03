"""
@project: multirag
@Author：龙
@file： init_ai_guard_system.py
@date：2025/01/11 17:30
@desc: 初始化AI安全护栏系统
"""

import logging
import os
import sys

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.db.db_models import SessionLocal
from api.db.services.guard_dimension_service import GuardDimensionService
from api.db.services.guard_label_service import GuardLabelService
from api.db.services.guard_library_service import GuardLibraryService
from api.db.services.guard_rule_service import GuardRuleService
from api.db.services.guard_service_service import GuardServiceService

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def init_ai_guard_system(tenant_id: str = None, created_by: str = None):
    """
    初始化AI安全护栏系统

    Args:
        tenant_id: 租户ID，如果为None则使用默认值
        created_by: 创建者ID，如果为None则使用默认值
    """
    # 使用默认值
    if not tenant_id:
        tenant_id = "default_tenant"
    if not created_by:
        created_by = "system"

    db = SessionLocal()
    try:
        logger.info(f"开始初始化AI安全护栏系统，租户: {tenant_id}")

        # 1. 初始化维度
        logger.info("初始化检测维度...")
        dimension_ids = GuardDimensionService.init_default_dimensions(db, tenant_id, created_by)
        logger.info(f"创建维度数量: {len(dimension_ids)}")

        # 建立维度代码到ID的映射
        dimensions = GuardDimensionService.get_dimensions_by_tenant(db, tenant_id)
        dimension_configs = {dim.code: dim.id for dim in dimensions}

        # 2. 初始化标签
        logger.info("初始化检测标签...")
        label_ids = GuardLabelService.init_default_labels(db, dimension_configs, tenant_id, created_by)
        logger.info(f"创建标签数量: {len(label_ids)}")

        # 建立标签代码到ID的映射
        labels = GuardLabelService.get_labels_by_tenant(db, tenant_id)
        label_configs = {label.code: label.id for label in labels}

        # 3. 初始化规则
        logger.info("初始化检测规则...")
        rule_ids = GuardRuleService.init_default_rules(db, label_configs, tenant_id, created_by)
        logger.info(f"创建规则数量: {len(rule_ids)}")

        # 4. 初始化词库
        logger.info("初始化词库...")
        library_ids = GuardLibraryService.init_default_libraries(db, tenant_id, created_by)
        logger.info(f"创建词库数量: {len(library_ids)}")

        # 5. 初始化服务配置
        logger.info("初始化服务配置...")
        service_ids = GuardServiceService.init_default_services(db, tenant_id, created_by)
        logger.info(f"创建服务数量: {len(service_ids)}")

        logger.info("AI安全护栏系统初始化完成!")

        # 打印初始化结果
        print("\n=== AI安全护栏系统初始化结果 ===")
        print(f"租户ID: {tenant_id}")
        print(f"创建者: {created_by}")
        print(f"维度数量: {len(dimension_ids)}")
        print(f"标签数量: {len(label_ids)}")
        print(f"规则数量: {len(rule_ids)}")
        print(f"词库数量: {len(library_ids)}")
        print(f"服务数量: {len(service_ids)}")

        # 列出创建的维度
        print("\n创建的维度:")
        for dim in dimensions:
            print(f"  - {dim.code}: {dim.name}")

        # 列出创建的标签
        print("\n创建的标签:")
        for label in labels:
            print(f"  - {label.code}: {label.name}")

        # 列出创建的服务
        services = GuardServiceService.get_services_by_tenant(db, tenant_id)
        print("\n创建的服务:")
        for service in services:
            print(f"  - {service.code}: {service.name}")

        print("\n系统初始化完成！")

    except Exception as e:
        logger.error(f"初始化AI安全护栏系统失败: {e}")
        db.rollback()
        raise
    finally:
        db.close()


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="初始化AI安全护栏系统")
    parser.add_argument("--tenant-id", type=str, help="租户ID")
    parser.add_argument("--created-by", type=str, help="创建者ID")

    args = parser.parse_args()

    try:
        init_ai_guard_system(tenant_id=args.tenant_id, created_by=args.created_by)
    except Exception as e:
        logger.error(f"初始化失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
