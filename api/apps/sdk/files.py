"""
@project: multirag
@file: sdk/files.py
@desc: 【已退役 / RETIRED】

本文件原先提供 SDK 文件管理接口（/api/v1/files/*、/api/v1/file/*）。这套接口已
RESTful 化并收编进 api/apps/restful_apis/file_api.py（对外路径不变 /api/v1，按
ragflow #13741 的路径设计对齐），鉴权由原 token_required 统一升级为 current_tenant_id
（web 会话 JWT + SDK API-key 皆可，SDK key 通过 fallback 依旧生效）。

为避免与 file_api.py 抢占同一批 /api/v1/files 路由（FastAPI 同 path+method 先注册者胜，
后者成死路由），本文件不再注册任何路由——保留空 router 仅作墓碑，便于追溯历史。
请勿在此新增端点；文件管理一律去 restful_apis/file_api.py。

收编后的路径映射（旧 sdk → 新 file_api，完全对齐 ragflow）：
  POST   /files/upload          -> POST   /files（multipart）
  POST   /files (create)        -> POST   /files（json）
  GET    /files                 -> GET    /files
  DELETE /files/delete          -> DELETE /files（file_ids -> ids）
  PUT    /files/{id}/name       -> POST   /files/move（new_name，mv 语义）
  PUT    /files/move            -> POST   /files/move（dest_file_id）
  GET    /files/{id}/download   -> GET    /files/{id}
  GET    /files/{id}/parent     -> GET    /files/{id}/parent
  GET    /files/{id}/parents    -> GET    /files/{id}/ancestors
原样保留（ragflow 无对应、multirag 专有，仍在 file_api.py）：
  POST /files/upload_info、GET /files/root、POST /file/convert、GET /file/download/{attachment_id}
"""
from fastapi import APIRouter

# 空 router：被自动发现机制加载但不挂载任何路由，避免与 file_api.py 路由冲突。
router = APIRouter()
