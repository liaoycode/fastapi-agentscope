from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.common.logger import getLogger

from app.core.exceptions.exception.business_exception import BusinessException

from datetime import datetime

from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

logger = getLogger()
def setup_exception_handlers(app: FastAPI):
    # 自定义异常处理器
    @app.exception_handler(BusinessException)
    def business_exception_handler(request: Request, exc: BusinessException):
        logger.error(exc.code,exc_info=exc.message)
        return __format_response(code=exc.code, message=exc.message)

    @app.exception_handler(404)
    def not_found_handler(request: Request, exc):
        return __format_response(
            code=404,
            message="接口不存在"
        )

    # HTTP异常处理器
    @app.exception_handler(StarletteHTTPException)
    def http_exception_handler(request: Request, exc: StarletteHTTPException):
        logger.error(f"HTTP {exc.status_code}: {exc.detail}", exc_info=True)
        return __format_response(code=exc.status_code, message=str(exc.detail))


    # 请求验证异常处理器
    @app.exception_handler(RequestValidationError)
    def validation_exception_handler(request: Request, exc: RequestValidationError):
        logger.error(f"Validation error: {exc.errors()}", exc_info=True)
        errors = []
        for error in exc.errors():
            if error["type"] == "missing":
                errors.append({
                    "field": ".".join(str(loc) for loc in error["loc"]),
                    "message": f"参数 {error['loc'][-1]} 为必填，请提供该参数",
                    "type": "missing"
                })
            else:
                errors.append({
                    "field": ".".join(str(loc) for loc in error["loc"]),
                    "message": error["msg"],
                    "type": error["type"]
                })

        return __format_response(code=status.HTTP_422_UNPROCESSABLE_ENTITY, message=str(errors))

    # ValueError异常处理器
    @app.exception_handler(ValueError)
    def value_error_handler(request: Request, exc: ValueError):
        logger.error(f"ValueError: {str(exc)}", exc_info=True)
        return __format_response(code=status.HTTP_422_UNPROCESSABLE_ENTITY, message=str(exc))

    # 通用异常处理器
    @app.exception_handler(Exception)
    def general_exception_handler(request: Request, exc: Exception):
        # 生产环境可以记录日志
        import traceback
        traceback.print_exc()

        return __format_response(code=status.HTTP_500_INTERNAL_SERVER_ERROR, message="服务器内部错误")

def __format_response(code, message, data=None):
    data = jsonable_encoder(
        data,
        custom_encoder={
            datetime: lambda dt: dt.strftime('%Y-%m-%d %H:%M:%S')
        }
    )
    return JSONResponse({'code': code, 'message': message, 'data': data})
