"""
@project: multirag
@file： dataset.py
@desc: 【已迁移】原 SDK 数据集接口已按 RESTful 风格重构为网关层 + 服务层
       （对标 ragflow #1db5409d）：
         - 网关层：api/apps/restful_apis/dataset_api.py
         - 服务层：api/apps/services/dataset_api_service.py
       对外 URL 仍为 /api/v1/datasets，保持不变。

       本文件保留空 router 占位，仅为兼容应用启动时的 sdk 路由自动加载逻辑，
       不再注册任何路由；待后续 SDK 整体重构时一并清理。
"""

from fastapi import APIRouter

router = APIRouter()
