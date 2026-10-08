"""AgentState <-> Postgres 持久化。

调用方持有 AsyncSession（FastAPI Depends 或脚本里直接拿），把 session
透传给 load_state / save_state 即可。这两个函数不管理事务边界。

序列化约定：
- 写：AgentState.model_dump(mode="json") -> dict -> JSONB
- 读：JSONB -> dict -> AgentState.model_validate(dict)
"""

import logging

from agentscope.message import (
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
)
from agentscope.state import AgentState
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.common.logger import getLogger
from app.infrastructure.repositories.chat_session_state_repo import (
    ChatSessionStateRepository,
)

logger = getLogger()


def _auto_deny_orphan_tool_calls(state: AgentState) -> None:
    """流中断可能让 state.context 末尾的 assistant 消息里出现
    state=ASKING（等用户确认）的 tool_call——下次 _reply 一进来
    _check_incoming_event 就会抛 "Agent is waiting for N tool calls
    but received no event"。

    处理：把 ASKING 的 tool_call 改成 DENIED，并补一条同 id 的
    ToolResultBlock(DENIED)，让消息序列保持平衡（LLM provider
    拒绝"tool call without result"），同时避免把没确认过的危险
    操作默默执行——agent._check_permission_impl 对 ALLOWED 会
    短路放行，对 DENIED 同样直接产生 DENIED 决策。
    """
    if not state.context:
        return
    last = state.context[-1]
    if last.role != "assistant":
        return
    denied = 0
    for b in list(last.content):
        if isinstance(b, ToolCallBlock) and b.state == ToolCallState.ASKING:
            tc_id = b.id
            tc_name = b.name
            b.state = ToolCallState.DENIED
            last.content.append(ToolResultBlock(
                id=tc_id,
                name=tc_name,
                output=[TextBlock(text="user disconnected before confirmation; denied")],
                state=ToolResultState.DENIED,
            ))
            denied += 1
    if denied:
        logger.warning(
            "denied %d orphan ASKING tool_call(s) on load (session=%s)",
            denied, state.session_id,
        )


async def load_state(
    session: AsyncSession,
    session_id: str,
) -> AgentState:
    """读不到记录时返回全新空 state（session_id 由调用方决定）。

    注意:不在这里 auto-deny 残留的 ASKING tool_call。
    调用方（router）拿到 state 后会判断:
      - context 末尾有 ASKING → 用户在回复确认,ASKING 必须原样保留给
        _build_resume_event 用,被 deny 就糟了
      - 否则 → 才安全地 deny 残留 ASKING（典型场景:上次断连）
    """
    repo = ChatSessionStateRepository(session)
    row = await repo.get_by_session_id(session_id)
    if row is None:
        return AgentState(session_id=session_id)
    payload = dict(row.state_json)
    payload["session_id"] = session_id
    try:
        state = AgentState.model_validate(payload)
    except Exception:
        logger.exception(
            "load_state: model_validate failed session=%s keys=%s",
            session_id, sorted(payload.keys()),
        )
        raise
    return state


async def save_state(
    session: AsyncSession,
    session_id: str,
    user_id: str,
    state: AgentState,
) -> None:
    """整段 state 全量覆盖写。流结束 / 中断前调用一次即可。"""
    repo = ChatSessionStateRepository(session)
    payload = state.model_dump(mode="json")
    await repo.upsert(
        session_id=session_id,
        user_id=user_id,
        state_json=payload,
    )
