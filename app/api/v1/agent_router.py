"""Agent SSE 入口：POST /api/v1/agent/chat。

为什么不用 Depends(get_session)：
    StreamingResponse 在路由函数返回后才开始迭代 async generator，
    而 FastAPI 的 Depends 清理（commit/close）在路由返回时立即执行——
    会话已经在流开始前就被关掉。这里手动管理 session 生命周期：
    load 在路由返回前做，save/commit/close 在 event_gen 的 finally 里做。
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolCallState, ToolResultBlock
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.common.logger import getLogger
from app.domain.services.session_service import (
    _auto_deny_orphan_tool_calls,
    load_state,
    save_state,
)
from app.infrastructure.agentscope.agent import chat_stream, _MSG_COMPLETE
from app.infrastructure.datasource import database as db
from app.infrastructure.repositories.chat_session_state_repo import (
    ChatSessionStateRepository,
)

logger = getLogger()
router = APIRouter(prefix="/agent", tags=["agent"])


class ChatRequest(BaseModel):
    session_id: str | None = Field(
        default=None,
        description="会话 ID；不传则服务端生成 UUID hex（同一 session_id 复用历史）",
    )
    user_id: str = Field(default="demo-user", description="用户标识")
    message: str = Field(..., description="本轮用户消息")


def _preview_text(context: list) -> str:
    """取上下文里第一条 user 消息作为会话预览。"""
    for m in context:
        if m.get("role") == "user":
            for b in m.get("content", []):
                if b.get("type") == "text" and b.get("text"):
                    return b["text"][:60]
    return "(空)"


@router.get("/sessions")
async def list_sessions(
    user_id: str = Query(default="demo-user", description="用户标识"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """按 user_id 列出会话（最新在前），前端列表用。"""
    async with db._session_factory() as session:
        repo = ChatSessionStateRepository(session)
        rows = await repo.list_by_user(user_id, limit=limit)
    return [
        {
            "session_id": r.session_id,
            "user_id": r.user_id,
            "updated_at": r.updated_at.isoformat(),
            "message_count": len(r.state_json.get("context", [])),
            "preview": _preview_text(r.state_json.get("context", [])),
            "has_summary": bool(r.state_json.get("summary")),
        }
        for r in rows
    ]


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """拉一个会话的完整消息历史（含 summary），前端打开会话时用。"""
    async with db._session_factory() as session:
        repo = ChatSessionStateRepository(session)
        row = await repo.get_by_session_id(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session_id": row.session_id,
        "user_id": row.user_id,
        "updated_at": row.updated_at.isoformat(),
        "summary": row.state_json.get("summary", ""),
        "messages": row.state_json.get("context", []),
    }


_CONFIRM_RE = re.compile(r"^(确认|同意|好|好的|ok|yes|y|是|确认执行|确认转账)$", re.IGNORECASE)
_DENY_RE = re.compile(r"^(取消|拒绝|no|n|不|否|不要|取消执行|取消转账)$", re.IGNORECASE)


def _has_pending_ask(state) -> bool:
    """state.context 末尾是否有一个 ASKING 的 tool_call 在等用户确认。"""
    if not state.context:
        return False
    last = state.context[-1]
    if last.role != "assistant":
        return False
    return any(
        isinstance(b, ToolCallBlock) and b.state == ToolCallState.ASKING
        for b in last.content
    )


def _build_resume_event(state, user_text: str) -> UserConfirmResultEvent:
    """把用户这一句"确认/取消"翻译成 UserConfirmResultEvent,含糊时默认拒绝。"""
    text = user_text.strip()
    if _CONFIRM_RE.match(text):
        confirmed = True
    else:
        # 匹配 "取消/拒绝" 之外的(包括不匹配任何关键字),都按拒绝处理
        # ——危险操作的安全默认;用户要继续就重发一句 "确认"
        confirmed = False
    results = []
    for b in state.context[-1].content:
        if isinstance(b, ToolCallBlock) and b.state == ToolCallState.ASKING:
            results.append(ConfirmResult(confirmed=confirmed, tool_call=b))
    return UserConfirmResultEvent(reply_id=state.reply_id, confirm_results=results)


@router.post("/chat")
async def chat(req: ChatRequest):
    session_id = req.session_id or uuid.uuid4().hex
    session: AsyncSession = db._session_factory()

    try:
        state = await load_state(session, session_id)
    except Exception:
        await session.close()
        raise

    # 检测是否在等用户确认:有 ASKING 的 tool_call 就把本句当 confirm/deny
    if _has_pending_ask(state):
        # 旧消息路径: 以前存在未确认的消息
        resume_event = _build_resume_event(state, req.message)
        # assistant(tool_result) → user("确认") → assistant("转账成功")。
        pending_user_msg = Msg(
            name=req.user_id,
            role="user",
            content=[TextBlock(text=req.message)],
        )
        user_msg = None
    else:
        # 新消息路径:此时 state.context 末尾若还有 ASKING tool_call,
        # 说明上次流中断残留(用户没回确认就关了窗口)——拒掉并补
        # ToolResultBlock,避免 _reply 一进来抛 "received no event"。
        # 注意:这条不能放在 load_state 里,否则用户在回复 pending ASK
        # 时也会被提前 deny,_build_resume_event 就拿不到 ASKING 了。
        _auto_deny_orphan_tool_calls(state)
        resume_event = None
        user_msg = Msg(
            name=req.user_id,
            role="user",
            content=[TextBlock(text=req.message)],
        )
        pending_user_msg = None

    async def event_gen():
        async def checkpoint_save():
            """把当前 state 落一次盘。失败仅记日志，不打断流。"""
            try:
                await save_state(session, session_id, req.user_id, state)
                await session.commit()
                logger.info(
                    "checkpoint: session=%s saved (%d msgs)",
                    session_id, len(state.context),
                )
            except Exception:
                logger.exception(
                    "checkpoint save failed for session=%s", session_id,
                )
                await session.rollback()

        try:
            # 先告诉客户端本次 session_id（哪怕他传了，也回一份以便日志对齐）
            # sse-starlette 接收 dict:顶层 key 必须是 "event"/"data"/"id"/"retry"
            # 之一,值会被序列化成标准 SSE 文本并自动加 \n\n 终止符——
            # 所以这里不用再手动拼 "data: ...\n\n" 那种字符串。
            yield {"event": "session", "data": session_id}
            async for raw in chat_stream(
                state, req.user_id,
                user_msg=user_msg,
                resume_event=resume_event,
            ):
                if raw is _MSG_COMPLETE:
                    # 一条 assistant Msg 已经写入 state.context，立刻落盘
                    await checkpoint_save()
                    continue
                # chat_stream 在流末尾 yield 一条 {"type": "usage", "data": ...}
                # 字典汇报本次 chat_stream 累计的 token 用量。
                # 与"普通字符串 delta"分支分开处理——dict 落到 SSE "data"
                # 通道里会让前端 streamText 多一段 JSON 文本。
                if isinstance(raw, dict) and raw.get("type") == "usage":
                    usage = raw["data"]
                    logger.info(
                        "chat usage session=%s calls=%d in=%d out=%d "
                        "cache_read=%d cache_write=%d",
                        session_id,
                        usage["model_calls"],
                        usage["input_tokens"],
                        usage["output_tokens"],
                        usage["cache_input_tokens"],
                        usage["cache_creation_input_tokens"],
                    )
                    # yield {
                    #     "event": "usage",
                    #     "data": json.dumps(
                    #         {"event": "usage", **usage,
                    #          "timestamp": datetime.now(timezone.utc).isoformat()},
                    #         ensure_ascii=False,
                    #     ),
                    # }
                    continue
                # 普通字符串(模型 delta 或 ASK 提示 HTML):整段发给前端,
                # 让前端原样拼到 streamText——必须保留 \n,否则 markdown
                # 表格/列表/代码块换行结构被破坏,marked.js 解析成纯文本。
                # 旧实现 raw.split("\n") 拆成多段再拼回,丢换行,
                # 于是表格这种"每行一个 |" 的语法在前端挤成一坨。
                if raw:
                    yield {
                        "data": json.dumps(
                            {
                                "event": "message",
                                "answer": raw,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                            ensure_ascii=False,
                        )
                    }
            # chat_stream 跑完后,state.context 末尾是 agent 新生成的回复 Msg,
            # 通常把 tool_call + 红字 prompt + tool_result + 成功文案合成一条
            # assistant Msg(实测 6 块)。tool_call 和 tool_result 不能拆到两条 Msg
            # (OpenAI provider 要求它们必须在同一条 assistant Msg,拆了会
            # 报 "tool call result does not follow tool call")。
            # 所以 user("确认") 不能整成独立 Msg——而是作为 TextBlock 塞进
            # 合并 Msg 内部,位置在红字 prompt 之后、tool_result 之前,
            # 视觉顺序就是:红字"即将转账..."→ 用户"确认"→ 转账成功。
            if pending_user_msg is not None:
                last = state.context[-1]
                if last.role == "assistant":
                    result_idx = next(
                        (i for i, b in enumerate(last.content)
                         if isinstance(b, ToolResultBlock)),
                        None,
                    )
                    if result_idx is not None:
                        last.content.insert(
                            result_idx,
                            TextBlock(text=f"用户回复：{req.message}"),
                        )
                    else:
                        # 兜底:这条 Msg 没有 tool_result(罕见),追加为独立 Msg
                        state.context.append(pending_user_msg)
                else:
                    state.context.append(pending_user_msg)
        except Exception as e:
            logger.exception("chat_stream failed for session=%s", session_id)
            yield {
                "event": "error",
                "data": json.dumps(
                    {"error": repr(e), "timestamp": datetime.now(timezone.utc).isoformat()},
                    ensure_ascii=False,
                ),
            }
        finally:
            # 兜底：流关闭前再存一次（应对没有 _Msg_COMPLETE 信号就中断的情况）
            await checkpoint_save()
            await session.close()

    return EventSourceResponse(event_gen())
