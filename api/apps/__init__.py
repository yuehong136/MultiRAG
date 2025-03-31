# coding=utf-8
"""
@project: multirag
@Author：龙
@file： apps.__init__.py
@date：2024/7/11 14:30
@desc:
"""
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi_login import LoginManager
from fastapi_login.exceptions import InvalidCredentialsException
from fastapi.security import OAuth2PasswordRequestForm

from sqlalchemy.orm import Session

from api.db.database import get_db, SessionLocal
from api.db.services import UserService
from api import settings
from errors.exceptions import AITranslateException
from api.constants import API_VERSION
from workflow_v2.workflow_exceptions import NodeExecutionError, WorkflowValidationError
from workflow_v2.workflow_state_manager import workflow_state_manager

description = """
Multi-RAG API helps you do awesome stuff. 🚀

## 本项目旨在构建一个多层次的系统，整合各类大语言模型（LLMs）、工具和服务，支持应用开发和API开放。
"""
tags_metadata = [
    {
        "name": "default",
        "description": "获取Auth Token",
    },
    {
        "name": "llm",
        "description": "模型相关接口",
        "externalDocs": {
            "description": "问题反馈",
            "url": "https://cake-doom-0c6.notion.site/LLM-1315b7c31d624827a9e5e067475d1a31?pvs=4",
        },
    },
]

# 在模块顶部（路由定义之前）创建线程池
executor = ThreadPoolExecutor(max_workers=20)  # 可以根据服务器性能调整

# 创建FastAPI实例
app = FastAPI(
    title="Multi-RAG",
    description=description,
    summary="大模型底座接口",
    version="0.0.1",
    terms_of_service="https://cake-doom-0c6.notion.site/4b6c4b3a5338497494620b3dd82e4acc?pvs=4",
    contact={
        "name": "DuXiaolong",
        "url": "https://github.com/yuehong136?tab=repositories",
        "email": "du13013901711@163.com",
    },
    license_info={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    },
    openapi_tags=tags_metadata,
    docs_url=None,
    redoc_url=None
)
# 添加处理CORS（跨域资源共享）的中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源的请求
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有HTTP方法
    allow_headers=["*"],  # 允许所有请求头
)
settings.init_settings()


# 添加启动和关闭事件
@app.on_event("startup")
async def startup_event():
    # 在FastAPI启动时启动状态管理器
    await workflow_state_manager.start()


@app.on_event("shutdown")
async def shutdown_event():
    # 在FastAPI关闭时关闭状态管理器
    await workflow_state_manager.shutdown()


# 初始化登录管理器，设置密钥和令牌URL
manager = LoginManager(settings.SECRET_KEY, token_url='/auth/token', default_expiry=timedelta(days=1))


# 定义一个函数，根据电子邮件加载用户
@manager.user_loader()
def load_user(email: str, db: Session = None):
    if db is None:
        db = SessionLocal()
        close_db = True
    else:
        close_db = False

    user = UserService.query_user_onlywith_email(db, email)

    if close_db:
        db.close()

    return user


# 定义一个函数，用于搜索API和应用页面的路径
def search_pages_path(pages_dir):
    app_path_list = [path for path in pages_dir.glob('*_app.py') if not path.name.startswith('.')]
    api_path_list = [path for path in pages_dir.glob('*_api.py') if not path.name.startswith('.')]
    app_path_list.extend(api_path_list)
    return app_path_list


# 定义一个函数，用于注册页面模块到FastAPI应用中
def register_page(page_path):
    path = f'{page_path}'
    page_name = page_path.stem.rstrip('_api') if "_api" in path else page_path.stem.rstrip('_app')
    module_name = '.'.join(page_path.parts[page_path.parts.index('api'):-1] + (page_name,))

    spec = spec_from_file_location(module_name, page_path)
    page = module_from_spec(spec)
    sys.modules[module_name] = page
    spec.loader.exec_module(page)
    url_prefix = (
        f"/api/{API_VERSION}" if "/sdk/" in path else f"/{API_VERSION}/{page_name}"
    )
    # 确保模块有 router 属性
    if hasattr(page, 'router'):
        app.include_router(page.router, prefix=url_prefix, tags=[page_name])
    else:
        logging.warning(f"Module {module_name} does not have 'router' attribute.")


# 定义要搜索页面的目录
pages_dir = [
    Path(__file__).parent,
    Path(__file__).parent.parent / 'api' / 'apps',
]

# 遍历页面目录，注册每个找到的页面
for dir in pages_dir:
    for path in search_pages_path(dir):
        register_page(path)


@app.exception_handler(AITranslateException)
async def ai_translate_exception_handler(request: Request, exc: AITranslateException):
    return JSONResponse(
        status_code=200,
        content={"status": "error", "message": exc.message},
    )


@app.exception_handler(NodeExecutionError)
async def node_execution_error_handler(request: Request, exc: NodeExecutionError):
    return JSONResponse(
        status_code=200,
        content={"status": "error",
                 "message": exc.message,
                 "workflow_exe_data": exc.workflow_exe_data}
    )


@app.exception_handler(WorkflowValidationError)
async def workflow_validation_error_handler(request: Request, exc: WorkflowValidationError):
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "message": exc.message,
        }
    )


# 定义FastAPI应用的自定义异常处理器
@app.exception_handler(Exception)
async def custom_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"message": str(exc)},
    )


# 定义一个用于用户登录的路由，生成和返回访问令牌
@app.post('/auth/token', summary="获取Access_token")
async def login(data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """
    此函数用于处理用户身份验证并生成访问令牌。[目前有效期：1天]

    参数:
    - data (OAuth2PasswordRequestForm): 依赖注入的OAuth2密码请求表单数据，包含用户名（电子邮件）和密码。
    - db (Session): 依赖注入的数据库会话，用于与数据库交互。

    返回:
    - dict: 包含访问令牌和令牌类型的信息字典，如{"access_token": "generated_token", "token_type": "bearer"}。

    异常:
    - InvalidCredentialsException: 如果提供的凭据无效或找不到用户，将引发此异常。

    使用场景:
    - 当客户端通过POST请求访问'/auth/token'端点时，此函数将被异步调用，以验证用户凭据并生成访问令牌。
    """
    # 提取请求中的用户名（电子邮件）和密码
    email = data.username
    password = data.password

    # 使用UserService查询数据库中是否存在该电子邮件的用户
    user = UserService.query_user(db, email, password)

    # 如果用户不存在，则抛出无效凭证异常
    if not user:
        raise InvalidCredentialsException

    # 创建访问令牌，其中包含用户的电子邮件作为主题（sub）
    access_token = manager.create_access_token(data={"sub": email})

    # 返回包含访问令牌和令牌类型的响应
    return {"access_token": access_token, "token_type": "bearer"}


# 定义一个简单的根端点，返回一条消息
@app.get("/", summary="根目录")
async def root():
    return {"message": "进入docs接口调试文档,请在地址后加/docs,需要标准doc文档,请在地址后加/redoc"}


# 如果此脚本被直接运行，则使用uvicorn启动FastAPI服务器
if __name__ == "__main__":
    import uvicorn

    # uvicorn.run("__init__:app", host="0.0.0.0", port=8000, reload=True)
    uvicorn.run("__init__:app", host="192.168.2.43", port=8123, reload=True)
