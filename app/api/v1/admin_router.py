"""Admin API:per-user 沙箱工作卷的检查/受控操作。

不做鉴权(本期)。内网场景假定由 nginx 兜底,公网部署需自行加 basic auth /
IP 限制,不要直接暴露。

端点:
  GET    /admin/volumes                 列出所有用户卷(含大小/容器状态)
  GET    /admin/volumes/{user_id}/tree  列文件树
  DELETE /admin/volumes/{user_id}/files 删若干 uploads/ 文件
  POST   /admin/volumes/{user_id}/cleanup 按 mtime 清旧 uploads/ 文件
  DELETE /admin/volumes/{user_id}       nuke:销毁整个工作卷(危险)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.common.logger import getLogger
from app.core.exceptions.exception.business_exception import BusinessException
from app.domain.services.sandbox_volume_inspector import (
    FileEntry,
    SandboxVolumeInspector,
    VolumeSummary,
)

logger = getLogger()
router = APIRouter(prefix="/admin", tags=["admin"])


class VolumeSummarySchema(BaseModel):
    user_id: str
    volume_name: str
    size_bytes: int
    file_count: int
    container_status: str

    @classmethod
    def from_model(cls, v: VolumeSummary) -> "VolumeSummarySchema":
        return cls(
            user_id=v.user_id,
            volume_name=v.volume_name,
            size_bytes=v.size_bytes,
            file_count=v.file_count,
            container_status=v.container_status,
        )


class FileEntrySchema(BaseModel):
    path: str
    name: str
    size_bytes: int
    mtime: str
    type: str

    @classmethod
    def from_model(cls, e: FileEntry) -> "FileEntrySchema":
        return cls(
            path=e.path, name=e.name,
            size_bytes=e.size_bytes,
            mtime=e.mtime, type=e.type,
        )


class DeleteFilesRequest(BaseModel):
    paths: list[str] = Field(
        ...,
        min_length=1,
        description="相对 workdir 的文件路径,必须以 ``uploads/`` 开头",
    )


def _inspector() -> SandboxVolumeInspector:
    return SandboxVolumeInspector()


@router.get("/volumes", response_model=list[VolumeSummarySchema])
async def list_volumes():
    """列出所有用户的卷(含大小、文件数、容器状态)。"""
    rows = await _inspector().list_all()
    return [VolumeSummarySchema.from_model(r) for r in rows]


@router.get(
    "/volumes/{user_id}/tree",
    response_model=list[FileEntrySchema],
)
async def get_volume_tree(
    user_id: str,
    path: str = Query("", description="相对 workdir 的子路径,空串=workdir 根"),
    max_depth: int = Query(4, ge=1, le=10),
):
    """列 user 卷内某子路径的文件树条目。"""
    entries = await _inspector().get_tree(user_id, path, max_depth)
    return [FileEntrySchema.from_model(e) for e in entries]


@router.delete("/volumes/{user_id}/files")
async def delete_files(user_id: str, req: DeleteFilesRequest):
    """删 user 卷内若干文件。path 必须以 ``uploads/`` 开头。"""
    try:
        result = await _inspector().delete_files(user_id, req.paths)
    except ValueError as e:
        # 路径非法 -> 400
        raise BusinessException(40012, str(e))
    except NotImplementedError as e:
        # docker 模式沙箱不在跑 -> 501 提示用户先起沙箱
        raise HTTPException(status_code=501, detail=str(e))
    logger.warning(
        "admin: delete_files user=%s paths=%s result=%s",
        user_id, req.paths, result,
    )
    return result


@router.post("/volumes/{user_id}/cleanup")
async def cleanup_old_files(
    user_id: str,
    older_than_days: int = Query(..., ge=0, le=3650),
):
    """删 ``uploads/`` 下 mtime 早于 ``older_than_days`` 天的文件。"""
    result = await _inspector().cleanup_old(user_id, older_than_days)
    logger.warning(
        "admin: cleanup user=%s days=%s deleted=%s",
        user_id, older_than_days, result.get("deleted"),
    )
    return result


@router.delete("/volumes/{user_id}")
async def nuke_volume(user_id: str):
    """销毁 user 的整个工作卷(危险,不可逆)。

    - docker: 停掉活跃沙箱容器 + 删除命名卷
    - local : 直接 rmtree ~/.agent-sandbox/<sanitized_uid>/
    """
    result = await _inspector().nuke(user_id)
    logger.warning(
        "admin: NUKE user=%s result=%s",
        user_id, result,
    )
    return result


__all__ = ["router"]