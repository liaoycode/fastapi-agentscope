"""文件上传 + 元数据查询路由。

POST /file/upload — multipart 上传到 per-user 沙箱 /sandbox/uploads/ (或 local 模式下的
~/.agent-sandbox/<user_id>/uploads/),同时写一个伴生 <file_id>.meta.json
用于跨 session 列出历史文件。

GET  /file/list   — 扫 uploads/*.meta.json 列出这个用户所有上传过的文件。

mode-aware 写入:
- docker: 临时 tar + container.put_archive (一次推两个文件,原子)
- local : 直接 Path.write_bytes (失败时回滚已写)
- none  : 拒绝
"""
import asyncio
import io
import json
import logging
import re
import shutil
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from app.core.common.logger import getLogger
from app.core.config.env_config import settings
from app.core.exceptions.exception.business_exception import BusinessException
from app.infrastructure.agentscope.sandbox.user_sandbox import (
    get_manager,
    get_user_workdir,
)

logger = getLogger()
router = APIRouter(prefix="/file", tags=["file"])


_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ALLOWED_EXTS_CACHE: set[str] | None = None


def _allowed_exts() -> set[str]:
    global _ALLOWED_EXTS_CACHE
    if _ALLOWED_EXTS_CACHE is None:
        _ALLOWED_EXTS_CACHE = {
            e.strip() for e in settings.upload_allowed_extensions.split(",")
            if e.strip()
        }
    return _ALLOWED_EXTS_CACHE


def _tar_two_files(file_id: str, ext: str, data: bytes, meta: dict) -> bytes:
    """把文件本体 + meta 打成 gzip tar,一次 put_archive 推两个文件 (原子)。

    tar 内容:
        <file_id><ext>            (实际文件)
        <file_id>.meta.json       (伴生元数据)
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # 1) 实际文件
        file_bytes = data
        info = tarfile.TarInfo(name=f"{file_id}{ext}")
        info.size = len(file_bytes)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(info, io.BytesIO(file_bytes))

        # 2) 伴生 meta
        meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        info2 = tarfile.TarInfo(name=f"{file_id}.meta.json")
        info2.size = len(meta_bytes)
        info2.mtime = info.mtime
        tar.addfile(info2, io.BytesIO(meta_bytes))
    return buf.getvalue()


async def _write_docker(user_id: str, file_id: str, ext: str, data: bytes, meta: dict) -> None:
    """docker 模式:tar + put_archive 一次推两个文件。"""
    mgr = get_manager()
    sb = await mgr.get_or_create(user_id)
    container = sb.workspace._workspace._container

    uploads_dir = f"{get_user_workdir(user_id)}/uploads"
    # put_archive 不会自动建父目录,首次上传先 mkdir
    # aiodocker 的 container.exec() 只构造对象不会跑,必须 .start() 才执行
    # 用 detach=False + 流读,既同步等结束也消费掉 stream,避免后续 put 抢拍
    mkdir_ex = await container.exec(["sh", "-c", f"mkdir -p {uploads_dir}"])
    stream = mkdir_ex.start(detach=False)
    while await stream.read_out() is not None:
        pass

    tar_bytes = await asyncio.to_thread(_tar_two_files, file_id, ext, data, meta)
    try:
        await container.put_archive(path=uploads_dir, data=tar_bytes)
    except Exception:
        logger.exception("put_archive failed user=%s file_id=%s", user_id, file_id)
        raise BusinessException(50050, "写入沙箱失败")


def _write_local(user_id: str, file_id: str, ext: str, data: bytes, meta: dict) -> None:
    """local 模式:分别 write_bytes,失败时回滚已写的。"""
    workdir = get_user_workdir(user_id)
    uploads = Path(workdir) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    target_file = uploads / f"{file_id}{ext}"
    target_meta = uploads / f"{file_id}.meta.json"
    try:
        target_file.write_bytes(data)
        target_meta.write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8",
        )
    except Exception:
        target_file.unlink(missing_ok=True)
        target_meta.unlink(missing_ok=True)
        raise


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    user_id: str = Form("demo-user"),
) -> dict:
    """上传文件到 per-user 沙箱 uploads/。

    流程:
    1. 校验扩展名 + 大小
    2. 生成 file_id + 读 bytes
    3. 写文件本体 + .meta.json (mode-aware 原子)
    4. 返回 {file_id, filename, size, ext}
    """
    workdir = get_user_workdir(user_id)
    if workdir is None:
        raise BusinessException(40020, "none 模式不支持文件上传")

    # 1) 扩展名校验
    if not file.filename:
        raise BusinessException(40010, "缺少文件名")
    ext = Path(file.filename).suffix.lower()
    if not ext:
        raise BusinessException(40010, "文件必须有扩展名")
    if ext not in _allowed_exts():
        raise BusinessException(
            40010, f"不支持的文件类型: {ext} (允许: {settings.upload_allowed_extensions})",
        )

    # 2) 大小校验 + 读
    data = await file.read()
    if len(data) > settings.upload_max_bytes:
        raise BusinessException(
            40011, f"文件过大: {len(data)} bytes (上限 {settings.upload_max_bytes})",
        )

    # 3) 原子写
    file_id = uuid.uuid4().hex
    meta = {
        "ext": ext,
        "original_name": file.filename[:255],  # 限长
        "size": len(data),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }

    mode = settings.agent_sandbox_mode
    if mode == "docker":
        await _write_docker(user_id, file_id, ext, data, meta)
    elif mode == "local":
        _write_local(user_id, file_id, ext, data, meta)
    else:
        # workdir 已经过滤过 none,这里 double-check
        raise BusinessException(40020, f"不支持的 sandbox 模式: {mode}")

    logger.info(
        "upload ok user=%s file_id=%s ext=%s size=%d mode=%s",
        user_id, file_id, ext, len(data), mode,
    )
    return {
        "file_id": file_id,
        "filename": file.filename,
        "size": len(data),
        "ext": ext,
    }


# ── /file/list ───────────────────────────────────────────────────────

_LIST_PYTHON_CMD = """
python3 -c "
import json, glob, os, sys
results = []
for p in sorted(glob.glob('{uploads_dir}/*.meta.json'),
                key=os.path.getmtime, reverse=True):
    try:
        with open(p, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        fid = os.path.basename(p)[:-len('.meta.json')]
        results.append({{'file_id': fid, **meta}})
    except Exception as e:
        print(f'SKIP {{p}}: {{e}}', file=sys.stderr)
for r in results:
    print(json.dumps(r, ensure_ascii=False))
"
"""


async def _list_in_docker(user_id: str, uploads_dir: str) -> list[dict]:
    mgr = get_manager()
    sb = await mgr.get_or_create(user_id)
    container = sb.workspace._workspace._container

    cmd = _LIST_PYTHON_CMD.format(uploads_dir=uploads_dir)
    ex = await container.exec(["sh", "-c", cmd])
    stream = ex.start(detach=False)
    raw = b""
    while True:
        msg = await stream.read_out()
        if msg is None:
            break
        if msg.stream == 1:
            raw += msg.data

    results = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping malformed list line: %s", line[:100])
    return results


def _list_in_local(uploads_dir: str) -> list[dict]:
    d = Path(uploads_dir)
    if not d.exists():
        return []
    results = []
    for mf in d.glob("*.meta.json"):
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
            results.append({
                "file_id": mf.name[:-len(".meta.json")],
                **meta,
            })
        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("skipping corrupt meta %s: %s", mf, e)
            continue
    results.sort(key=lambda x: x.get("uploaded_at", ""), reverse=True)
    return results


@router.get("/list")
async def list_files(user_id: str = Query("demo-user")) -> list[dict]:
    """列出这个用户所有上传过的文件 (从 .meta.json 读)。"""
    workdir = get_user_workdir(user_id)
    if workdir is None:
        return []
    uploads_dir = f"{workdir}/uploads"

    mode = settings.agent_sandbox_mode
    if mode == "docker":
        return await _list_in_docker(user_id, uploads_dir)
    if mode == "local":
        return _list_in_local(uploads_dir)
    return []