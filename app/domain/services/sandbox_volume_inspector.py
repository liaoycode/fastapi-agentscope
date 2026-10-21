"""per-user 沙箱工作卷的检查/受控操作。

mode-aware:
  - docker:  走 aiodocker 查询命名卷 + 在容器内 exec
  - local :  走 host 目录 ~/.agent-sandbox/<sanitized_uid>/
  - none  :  所有方法返回空/抛错

设计目标:不依赖沙箱是否在跑 —— get_tree 路径会复用活跃沙箱的容器,
没有活跃容器时(只有 docker mode)也能起临时容器挂同一命名卷读。

不做硬 size 限制;只把可观测性和按需操作暴露给 admin。生产场景
由外部监控(docker system df)触发告警,运维通过这套 API 自助处理。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.common.logger import getLogger
from app.core.config.env_config import settings
from app.infrastructure.agentscope.sandbox.docker_client import (
    DockerUnavailableError,
    make_docker_client,
)
from app.infrastructure.agentscope.sandbox.user_sandbox import (
    get_manager,
    sanitize_user_id,
)

logger = getLogger()


# ── 数据结构 ───────────────────────────────────────────────────────


@dataclass
class VolumeSummary:
    user_id: str
    volume_name: str
    size_bytes: int
    file_count: int
    container_status: str  # "running" | "stopped" | "absent"


@dataclass
class FileEntry:
    path: str       # 相对 workdir 的路径,如 "uploads/foo.csv"
    name: str
    size_bytes: int
    mtime: str       # ISO-8601 UTC
    type: str        # "file" | "dir"


# ── 入口类 ────────────────────────────────────────────────────────


class SandboxVolumeInspector:
    """per-user 工作卷的检查/受控操作。"""

    def __init__(self) -> None:
        self._mode = settings.agent_sandbox_mode

    # ── list_all ────────────────────────────────────────────────

    async def list_all(self) -> list[VolumeSummary]:
        """列出所有用户的卷 + 大小/文件数/容器状态。"""
        if self._mode == "local":
            return await self._list_all_local()
        if self._mode == "docker":
            return await self._list_all_docker()
        return []

    async def _list_all_local(self) -> list[VolumeSummary]:
        root = Path(os.path.expanduser(settings.agent_local_sandbox_root))
        if not root.exists():
            return []
        results: list[VolumeSummary] = []
        manager = get_manager()
        active_users = set(manager._sandboxes.keys())
        for user_dir in sorted(root.iterdir()):
            if not user_dir.is_dir():
                continue
            sanitized = user_dir.name
            size, count = await asyncio.to_thread(_dir_size_count, user_dir)
            results.append(VolumeSummary(
                user_id=sanitized,
                volume_name=str(user_dir),
                size_bytes=size,
                file_count=count,
                container_status="running" if sanitized in active_users else "stopped",
            ))
        results.sort(key=lambda v: v.size_bytes, reverse=True)
        return results

    async def _list_all_docker(self) -> list[VolumeSummary]:
        try:
            client = make_docker_client()
        except DockerUnavailableError:
            return []
        try:
            try:
                resp = await client.volumes.list(
                    filters={"name": [f"{settings.agent_user_volume_prefix}-"]},
                )
            except Exception:
                logger.exception("list_all: docker volume list failed")
                return []
            volumes = resp.get("Volumes") or []
            # 活跃容器 user 集合
            manager = get_manager()
            active_users = set(manager._sandboxes.keys())
            prefix = f"{settings.agent_user_volume_prefix}-"
            results: list[VolumeSummary] = []
            for v in volumes:
                name = v.get("Name") or ""
                if not name.startswith(prefix):
                    continue
                sanitized = name[len(prefix):]
                # docker volume 没有 size 字段 —— 起临时容器 du 一下。
                # 临时容器用 alpine 镜像(几乎必有),没有就走官方 fallback。
                size, count = await self._du_in_temp_container(
                    client, name,
                    workdir=settings.agent_workspace_container_dir,
                )
                results.append(VolumeSummary(
                    user_id=sanitized,
                    volume_name=name,
                    size_bytes=size,
                    file_count=count,
                    container_status="running" if sanitized in active_users else "stopped",
                ))
            results.sort(key=lambda v: v.size_bytes, reverse=True)
            return results
        finally:
            try:
                await client.close()
            except Exception:
                pass

    # ── get_tree ────────────────────────────────────────────────

    async def get_tree(
        self,
        user_id: str,
        path: str = "",
        max_depth: int = 4,
    ) -> list[FileEntry]:
        """列 user 卷内 path 的文件树条目(path 相对 workdir,空串 = workdir 根)。"""
        sanitized = sanitize_user_id(user_id)
        if self._mode == "local":
            return await asyncio.to_thread(
                _walk_local_tree,
                self._local_workdir(sanitized),
                path, max_depth,
            )
        if self._mode == "docker":
            return await self._get_tree_docker(sanitized, path, max_depth)
        return []

    async def _get_tree_docker(
        self,
        sanitized: str,
        path: str,
        max_depth: int,
    ) -> list[FileEntry]:
        # 优先复用活跃沙箱 —— 不重新起容器
        manager = get_manager()
        sb = manager._sandboxes.get(sanitized)
        if sb is not None:
            return await self._exec_tree_in_container(
                sb.workspace._container,
                settings.agent_workspace_container_dir,
                path, max_depth,
            )
        # 沙箱没起:起临时容器挂同一命名卷
        try:
            client = make_docker_client()
        except DockerUnavailableError:
            return []
        try:
            volume_name = (
                f"{settings.agent_user_volume_prefix}-{sanitized}"
            )
            entries = await self._exec_tree_in_temp_container(
                client, volume_name,
                settings.agent_workspace_container_dir,
                path, max_depth,
            )
            return entries
        finally:
            try:
                await client.close()
            except Exception:
                pass

    # ── delete_files ────────────────────────────────────────────

    async def delete_files(
        self,
        user_id: str,
        paths: list[str],
    ) -> dict:
        """删 user 卷内若干文件。path 必须以 ``uploads/`` 开头(防误删 skills)。"""
        sanitized = sanitize_user_id(user_id)
        for p in paths:
            _assert_uploads_path(p)
        if self._mode == "local":
            return await asyncio.to_thread(
                _delete_files_local,
                self._local_workdir(sanitized), paths,
            )
        if self._mode == "docker":
            return await self._delete_files_docker(sanitized, paths)
        return {"deleted": 0, "missing": list(paths)}

    async def _delete_files_docker(
        self,
        sanitized: str,
        paths: list[str],
    ) -> dict:
        workdir = settings.agent_workspace_container_dir
        # 拼成单条 sh -c,每个路径加单引号防 shell 注入
        quoted = " ".join(f"rm -f '{workdir}/{p}'" for p in paths)
        cmd = f"{quoted} && echo __OK__"
        manager = get_manager()
        sb = manager._sandboxes.get(sanitized)
        if sb is not None:
            return await self._exec_rm_in_container(
                sb.workspace._container, paths,
            )
        # 没活跃沙箱:起临时容器删
        try:
            client = make_docker_client()
        except DockerUnavailableError:
            return {"deleted": 0, "missing": list(paths)}
        try:
            volume_name = f"{settings.agent_user_volume_prefix}-{sanitized}"
            return await self._exec_rm_in_temp_container(
                client, volume_name, workdir, paths,
            )
        finally:
            try:
                await client.close()
            except Exception:
                pass

    # ── cleanup_old ─────────────────────────────────────────────

    async def cleanup_old(
        self,
        user_id: str,
        older_than_days: int,
    ) -> dict:
        """删 ``uploads/`` 下 mtime 早于 N 天的文件。"""
        if older_than_days < 0:
            raise ValueError("older_than_days must be >= 0")
        sanitized = sanitize_user_id(user_id)
        if self._mode == "local":
            return await asyncio.to_thread(
                _cleanup_old_local,
                self._local_workdir(sanitized), older_than_days,
            )
        if self._mode == "docker":
            return await self._cleanup_old_docker(sanitized, older_than_days)
        return {"deleted": 0}

    async def _cleanup_old_docker(
        self,
        sanitized: str,
        older_than_days: int,
    ) -> dict:
        workdir = settings.agent_workspace_container_dir
        cmd = (
            f"find {workdir}/uploads -type f -mtime +{older_than_days} "
            f"-print -delete | wc -l"
        )
        manager = get_manager()
        sb = manager._sandboxes.get(sanitized)
        if sb is not None:
            stdout = await _exec_collect_stdout(sb.workspace._container, cmd)
        else:
            try:
                client = make_docker_client()
            except DockerUnavailableError:
                return {"deleted": 0}
            try:
                volume_name = f"{settings.agent_user_volume_prefix}-{sanitized}"
                stdout = await self._exec_in_temp_container(
                    client, volume_name, cmd,
                )
            finally:
                try:
                    await client.close()
                except Exception:
                    pass
        try:
            n = int(stdout.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            n = 0
        logger.info(
            "cleanup_old: user=%s days=%d deleted=%d",
            sanitized, older_than_days, n,
        )
        return {"deleted": n}

    # ── nuke ────────────────────────────────────────────────────

    async def nuke(self, user_id: str) -> dict:
        """销毁 user 的整个工作卷(危险,需显式调用)。"""
        sanitized = sanitize_user_id(user_id)
        manager = get_manager()
        await manager.close(sanitized)  # 容器起来的话先关掉

        if self._mode == "local":
            return await asyncio.to_thread(
                _nuke_local,
                self._local_workdir(sanitized),
            )
        if self._mode == "docker":
            return await self._nuke_docker(sanitized)
        return {"deleted": False}

    async def _nuke_docker(self, sanitized: str) -> dict:
        volume_name = f"{settings.agent_user_volume_prefix}-{sanitized}"
        try:
            client = make_docker_client()
        except DockerUnavailableError:
            return {"deleted": False, "reason": "docker unavailable"}
        try:
            try:
                vol = await client.volumes.get(volume_name)
                await vol.delete(force=True)
                logger.info("nuke: deleted volume %s", volume_name)
                return {"deleted": True, "volume": volume_name}
            except Exception as e:
                # 404 = 早就不存在,算成功
                if getattr(e, "status", None) == 404:
                    return {"deleted": True, "volume": volume_name,
                            "note": "already absent"}
                logger.exception("nuke: delete volume failed")
                return {"deleted": False, "reason": repr(e)}
        finally:
            try:
                await client.close()
            except Exception:
                pass

    # ── 临时容器辅助(docker mode,沙箱不在跑时用) ──────────────

    async def _du_in_temp_container(
        self,
        client,
        volume_name: str,
        workdir: str,
    ) -> tuple[int, int]:
        size_raw = await self._exec_in_temp_container(
            client, volume_name,
            f"du -sb {workdir} 2>/dev/null | awk '{{print $1}}'",
        )
        count_raw = await self._exec_in_temp_container(
            client, volume_name,
            f"find {workdir} -type f 2>/dev/null | wc -l",
        )
        try:
            size = int(size_raw.decode().strip().splitlines()[-1] or "0")
        except (ValueError, IndexError):
            size = 0
        try:
            count = int(count_raw.decode().strip().splitlines()[-1] or "0")
        except (ValueError, IndexError):
            count = 0
        return size, count

    async def _exec_in_temp_container(
        self,
        client,
        volume_name: str,
        cmd: str,
        image: str = "alpine:latest",
    ) -> bytes:
        """起一个一次性容器,挂载给定命名卷,exec 后销毁。"""
        config = {
            "Image": image,
            "Cmd": ["sh", "-c", cmd],
            "AttachStdout": False,
            "AttachStderr": False,
            "HostConfig": {
                "Binds": [f"{volume_name}:{settings.agent_workspace_container_dir}:ro"],
            },
        }
        try:
            container = await client.containers.create_or_replace(
                config=config,
                name=f"as_inspect_{_uid()}",
            )
        except Exception:
            # alpine 镜像可能没拉过;用 docker 镜像作为 fallback
            try:
                container = await client.containers.create_or_replace(
                    config={**config, "Image": "docker:7"},
                    name=f"as_inspect_{_uid()}",
                )
            except Exception:
                logger.exception(
                    "temp container create failed (vol=%s)", volume_name,
                )
                return b""
        try:
            await container.start()
            # 一次性命令退出会让容器直接 stopped;wait 等它结束
            try:
                await container.wait(timeout=30)
            except Exception:
                pass
            # 拉 stdout —— aiodocker containers 没有 exec 接口(那是
            # execs.create + start),退而用 logs 取全部输出
            try:
                stream = container.log(stdout=True, stderr=False, follow=False)
                if hasattr(stream, "__aiter__"):
                    chunks = []
                    async for chunk in stream:
                        chunks.append(chunk)
                    return b"".join(chunks)
                # sync result
                return stream if isinstance(stream, bytes) else b""
            except Exception:
                logger.exception("temp container log read failed")
                return b""
        finally:
            try:
                await container.delete(force=True)
            except Exception:
                pass

    async def _exec_tree_in_container(
        self,
        container,
        workdir: str,
        path: str,
        max_depth: int,
    ) -> list[FileEntry]:
        target = f"{workdir}/{path}" if path else workdir
        cmd = (
            f"find {target} -maxdepth {max_depth} "
            f"\\( -type f -o -type d \\) "
            f"-printf '%y\\t%T@\\t%s\\t%p\\n' 2>/dev/null"
        )
        raw = await _exec_collect_stdout(container, cmd)
        return _parse_find_output(raw, workdir)

    async def _exec_tree_in_temp_container(
        self,
        client,
        volume_name: str,
        workdir: str,
        path: str,
        max_depth: int,
    ) -> list[FileEntry]:
        target = f"{workdir}/{path}" if path else workdir
        # 临时容器读 named volume 模式有限(alpine 的 find 也支持 -printf),
        # 不用 install busybox。
        cmd = (
            f"find {target} -maxdepth {max_depth} "
            f"\\( -type f -o -type d \\) "
            f"-printf '%y\\t%T@\\t%s\\t%p\\n' 2>/dev/null"
        )
        raw = await self._exec_in_temp_container(client, volume_name, cmd)
        return _parse_find_output(raw, workdir)

    async def _exec_rm_in_container(
        self,
        container,
        paths: list[str],
    ) -> dict:
        workdir = settings.agent_workspace_container_dir
        # 先 dry-run 检查哪些在,再真删,返回 missing
        check_cmd = (
            "for p in " + " ".join(f"'{workdir}/{p}'" for p in paths) + "; do "
            "[ -e \"$p\" ] && echo EXIST || echo MISSING; done"
        )
        check_out = await _exec_collect_stdout(container, check_cmd)
        existing = []
        missing = []
        for i, line in enumerate(check_out.decode().splitlines()):
            tag = line.strip()
            if tag == "EXIST":
                existing.append(paths[i])
            elif tag == "MISSING":
                missing.append(paths[i])
        if not existing:
            return {"deleted": 0, "missing": missing}
        rm_cmd = "rm -f " + " ".join(f"'{workdir}/{p}'" for p in existing)
        await _exec_collect_stdout(container, rm_cmd)
        return {"deleted": len(existing), "missing": missing}

    async def _exec_rm_in_temp_container(
        self,
        client,
        volume_name: str,
        workdir: str,
        paths: list[str],
    ) -> dict:
        # 临时容器只读挂载,删不了 —— 走"起一个 RW 临时容器"路径
        # 实际把 commands 也改成覆盖模式太复杂,直接 throw 让上层用 nuke
        raise NotImplementedError(
            "delete_files on offline sandbox requires an active sandbox; "
            "start one first or use nuke",
        )

    # ── helpers ────────────────────────────────────────────────

    def _local_workdir(self, sanitized: str) -> Path:
        root = Path(os.path.expanduser(settings.agent_local_sandbox_root))
        return root / sanitized


# ── 模块级工具函数 ─────────────────────────────────────────────────


def _uid() -> str:
    """短随机串,临时容器名后缀用。"""
    import uuid
    return uuid.uuid4().hex[:8]


def _assert_uploads_path(p: str) -> None:
    """path 必须以 ``uploads/`` 开头(防误删 skills/snapshot)。"""
    if not p or ".." in p or p.startswith("/"):
        raise ValueError(f"unsafe path: {p!r}")
    if not p.startswith("uploads/"):
        raise ValueError(
            f"path must be under uploads/: {p!r} "
            "(refusing to touch skills/snapshot)",
        )


def _dir_size_count(root: Path) -> tuple[int, int]:
    """递归统计目录总字节数和文件数。"""
    total = 0
    count = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.stat(fp)
                total += st.st_size
                count += 1
            except OSError:
                pass
    return total, count


def _walk_local_tree(
    root: Path, rel: str, max_depth: int,
) -> list[FileEntry]:
    """递归走 host 目录,产出 FileEntry 列表。"""
    if not root.exists():
        return []
    target = (root / rel).resolve() if rel else root
    if not target.exists():
        return []
    base_depth = len(target.relative_to(root).parts) if rel else 0
    entries: list[FileEntry] = []
    for dirpath, dirs, files in os.walk(target):
        depth = len(Path(dirpath).relative_to(root).parts) - base_depth
        if depth > max_depth:
            dirs.clear()
            continue
        # 先收集 dir(排在前)
        for d in sorted(dirs):
            full = Path(dirpath) / d
            try:
                st = full.stat()
            except OSError:
                continue
            rpath = str(full.relative_to(root))
            entries.append(FileEntry(
                path=rpath,
                name=d,
                size_bytes=0,
                mtime=datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc,
                ).isoformat(),
                type="dir",
            ))
        for f in sorted(files):
            full = Path(dirpath) / f
            try:
                st = full.stat()
            except OSError:
                continue
            rpath = str(full.relative_to(root))
            entries.append(FileEntry(
                path=rpath,
                name=f,
                size_bytes=st.st_size,
                mtime=datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc,
                ).isoformat(),
                type="file",
            ))
    return entries


def _delete_files_local(root: Path, paths: list[str]) -> dict:
    deleted = 0
    missing: list[str] = []
    for p in paths:
        full = (root / p).resolve()
        # 防止 p 含 .. 逃出 root
        if not str(full).startswith(str(root.resolve())):
            missing.append(p)
            continue
        try:
            if full.exists():
                if full.is_dir():
                    shutil.rmtree(full)
                else:
                    full.unlink()
                deleted += 1
            else:
                missing.append(p)
        except OSError:
            missing.append(p)
    return {"deleted": deleted, "missing": missing}


def _cleanup_old_local(root: Path, days: int) -> dict:
    uploads = root / "uploads"
    if not uploads.exists():
        return {"deleted": 0}
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    deleted = 0
    for dirpath, _dirs, files in os.walk(uploads):
        for fn in files:
            fp = Path(dirpath) / fn
            try:
                if datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc) < cutoff:
                    fp.unlink()
                    deleted += 1
            except OSError:
                pass
    return {"deleted": deleted}


def _nuke_local(root: Path) -> dict:
    if root.exists():
        shutil.rmtree(root)
        return {"deleted": True, "path": str(root)}
    return {"deleted": False, "reason": "path not found"}


def _parse_find_output(raw: bytes, workdir: str) -> list[FileEntry]:
    """解析 ``find -printf '%y\\t%T@\\t%s\\t%p\\n'`` 输出。

    %y: f=file, d=dir
    %T@: mtime as float seconds since epoch
    %s: size in bytes
    %p: 完整路径
    """
    entries: list[FileEntry] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        type_char, mtime_s, size_s, fullpath = parts
        type_str = "dir" if type_char == "d" else "file"
        try:
            mtime = datetime.fromtimestamp(float(mtime_s), tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            mtime = ""
        try:
            size = int(size_s)
        except ValueError:
            size = 0
        rel = fullpath
        if rel.startswith(workdir + "/"):
            rel = rel[len(workdir) + 1:]
        elif rel == workdir:
            rel = ""
        name = rel.rsplit("/", 1)[-1] if rel else workdir.rsplit("/", 1)[-1]
        entries.append(FileEntry(
            path=rel, name=name, size_bytes=size,
            mtime=mtime, type=type_str,
        ))
    return entries


async def _exec_collect_stdout(container, cmd: str) -> bytes:
    """exec 一条 sh 命令,收集 stdout。

    从 snapshot.py 复刻的薄壳;inspector 跟 snapshot 在不同模块,
    直接 copy 一份避免 service→infrastructure 的反向依赖。
    """
    ex = await container.exec(["sh", "-c", cmd])
    stream = ex.start(detach=False)
    pieces: list[bytes] = []
    while True:
        msg = await stream.read_out()
        if msg is None:
            break
        if msg.stream == 1:
            pieces.append(msg.data)
    return b"".join(pieces)


__all__ = [
    "SandboxVolumeInspector",
    "VolumeSummary",
    "FileEntry",
]