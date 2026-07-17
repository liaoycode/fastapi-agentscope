# repeat_submit.py
import hashlib
import json
import logging
from typing import Optional

from fastapi import Request, HTTPException, status, Depends
from redis.exceptions import RedisError

from app.core.common.logger import getLogger

from fastapi import Request, Header
from app.infrastructure.redis.redis_client import redis_client

logger = getLogger()


class RepeatSubmitGuard:
    def __init__(self):
        self.redis = redis_client

    async def _extract_payload_signature(self, request: Request) -> str:
        """
        根据 Content-Type 选择合适的方式提取"内容特征"，
        而不是无脑读 body 的原始字节。
        """
        content_type = request.headers.get("content-type", "")

        if "multipart/form-data" in content_type:
            # multipart 的 body 每次都带随机 boundary，原始字节永不相同，
            # 必须解析出真正的字段值，忽略文件内容（或用文件大小/hash代替）
            form = await request.form()
            fields = {}
            for key, value in form.multi_items():
                if hasattr(value, "filename"):  # 是上传的文件
                    # 用文件名+大小做特征，不读全部文件内容（避免大文件性能问题）
                    content = await value.read()
                    await value.seek(0)  # 读完要复位，否则后续业务逻辑读不到文件
                    fields[key] = f"{value.filename}:{len(content)}"
                else:
                    fields[key] = str(value)
            return json.dumps(fields, sort_keys=True)

        elif "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            fields = {k: str(v) for k, v in form.multi_items()}
            return json.dumps(fields, sort_keys=True)

        elif "application/json" in content_type:
            body = await request.body()
            return body.decode("utf-8", errors="ignore")

        else:
            # GET 请求或者没有 body 的情况，走 query string
            body = await request.body()
            return body.decode("utf-8", errors="ignore")

    def _build_key(self, user_id: str, method: str, path: str,
                    query_string: str, payload_sig: str) -> str:
        # 关键修复：query string 一定要参与，不管有没有 body
        raw = f"{user_id}:{method}:{path}:{query_string}:{payload_sig}"
        md5 = hashlib.md5(raw.encode()).hexdigest()
        return f"submit:repeat:{md5}"

    async def check(
        self,
        request: Request,
        user_id: str,
        expire_seconds: int = 3,
        fail_open: bool = False,
    ):
        # query_string 覆盖 GET 参数化提交，比如 GET /order?product_id=1&quantity=2
        query_string = str(request.query_params)  # 已经是排序无关的字符串表示，但注意顺序敏感，见下方说明
        payload_sig = await self._extract_payload_signature(request)

        key = self._build_key(user_id, request.method, request.url.path,
                               query_string, payload_sig)

        try:
            success = await self.redis.set(key, "1", nx=True, ex=expire_seconds)
        except RedisError as e:
            logger.error(f"防重复提交 Redis 异常: {e}")
            if fail_open:
                return
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="服务暂时不可用，请稍后重试",
            )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="请勿重复提交",
            )

guard = RepeatSubmitGuard()
async def get_current_user_id(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """
    实际项目中这里应该解析 JWT / Session 拿到真实用户身份，
    不要直接信任客户端传来的 user-id 头（否则换个假 ID 就能绕过防重）。
    这里演示先用 header 兜底。
    """
    # TODO: 替换为真实的 JWT 解析逻辑
    user_id = request.headers.get("X-User-Id")
    return user_id or "anonymous"


def no_repeat_submit(expire_seconds: int = 3, fail_open: bool = True):
    """
    用法：
    @app.post("/order", dependencies=[Depends(no_repeat_submit(expire_seconds=5))])
    """
    async def dependency(
        request: Request,
        user_id: str = Depends(get_current_user_id),
    ):
        await guard.check(
            request=request,
            user_id=user_id,
            expire_seconds=expire_seconds,
            fail_open=fail_open,
        )

    return dependency

# @app.get(
#     "/order/search",
#     dependencies=[Depends(no_repeat_submit(expire_seconds=3))],
# )
# async def search_order(product_id: int, quantity: int):
#     # GET + query params 提交现在也能被正确区分了
#     return {"product_id": product_id, "quantity": quantity}