from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import logging


class AdminException(Exception):
    """管理后台基础异常类"""
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.type = "admin"
        self.code = code
        self.message = message


class UserNotFoundError(AdminException):
    """用户不存在异常"""
    def __init__(self, username: str):
        super().__init__(f"User '{username}' not found", 404)


class UserAlreadyExistsError(AdminException):
    """用户已存在异常"""
    def __init__(self, username: str):
        super().__init__(f"User '{username}' already exists", 409)


class CannotDeleteAdminError(AdminException):
    """不能删除管理员异常"""
    def __init__(self):
        super().__init__("Cannot delete admin account", 403)


class AuthenticationError(AdminException):
    """认证失败异常"""
    def __init__(self, message: str = "Authentication failed"):
        super().__init__(message, 401)


def setup_exception_handlers(app: FastAPI):
    """设置全局异常处理器"""
    
    @app.exception_handler(AdminException)
    async def admin_exception_handler(request: Request, exc: AdminException):
        """处理 AdminException 异常"""
        logging.error(f"AdminException: {exc.message}")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "code": exc.code,
                "message": exc.message,
                "data": None
            }
        )
    
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """处理请求验证异常"""
        logging.error(f"Validation error: {exc.errors()}")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "code": 400,
                "message": f"Validation error: {exc.errors()}",
                "data": None
            }
        )
    
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        """处理 HTTP 异常"""
        logging.error(f"HTTP error: {exc.detail}")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "code": exc.status_code,
                "message": exc.detail,
                "data": None
            }
        )
    
    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        """处理通用异常"""
        logging.error(f"Unexpected error: {str(exc)}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "code": 500,
                "message": f"Internal server error: {str(exc)}",
                "data": None
            }
        )
