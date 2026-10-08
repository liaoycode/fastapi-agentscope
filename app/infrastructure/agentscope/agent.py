import json
import logging
import threading

from agentscope.message import Msg as _Msg
from agentscope.event import (
    ModelCallEndEvent,
    RequireUserConfirmEvent,
    ToolCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    TextBlockDeltaEvent,
    ToolResultTextDeltaEvent,
)

from agentscope.message import Msg
from agentscope.message import TextBlock
from agentscope.tool import Toolkit
from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel
from agentscope.skill import SkillLoaderBase
from pydantic import SecretStr

from app.core.common.logger import getLogger
from app.core.config.env_config import settings
from app.infrastructure.agentscope.skill.skill_loader import build_skill_loaders
from agentscope.permission import (
    PermissionContext,
    PermissionMode,
)

from agentscope.agent import Agent, ContextConfig
from agentscope.agent._agent import AgentState

from app.infrastructure.agentscope.prompt import load_prompt
from app.infrastructure.agentscope.sandbox.user_sandbox import (
    LazyBash,
    get_manager,
)
from app.infrastructure.tools import ALL_TOOLS

# 当 chat_stream 处理完一个完整 Msg(即 assistant 的一条回答)后 yield 这个对象,
# 调用方可以据此触发 save_state——这样用户问的问题 + 模型给的回答一落地就存,
# 不必等 HTTP 流关掉。sentinel 对象保证不会被误判成普通字符串。
_MSG_COMPLETE = object()
logger = getLogger()

# 进程级单例:model 无 per-user 差异,复用即可;
# skill loader 按 user_id 缓存(personal skill 是用户维度的)。
# sandbox / bash_tool 改为 per-user,由 UserSandboxManager 懒创建。
_model: OpenAIChatModel | None = None
_skill_loaders_cache: dict[str, list[SkillLoaderBase]] = {}
_lock = threading.Lock()

# 启动时把默认 prompt 加载一次:文件缺失/格式错误就让进程直接挂,
# 不要等到第一个 chat 请求进来才发现 prompt 没配置。
_DEFAULT_PROMPT_NAME = settings.agent_system_prompt
_DEFAULT_PROMPT = load_prompt(_DEFAULT_PROMPT_NAME)


def _get_model() -> OpenAIChatModel:
    global _model
    if _model is None:
        _model = OpenAIChatModel(
            credential=OpenAICredential(
                api_key=SecretStr(settings.llm_api_key),
                base_url=settings.llm_base_url,
            ),
            model=settings.llm_model_name,
            context_size=settings.llm_context_size,
        )
    return _model


def _get_skill_loaders(user_id: str) -> list[SkillLoaderBase]:
    # 读路径无锁(dict.get 是原子的),写路径加锁
    loaders = _skill_loaders_cache.get(user_id)
    if loaders is not None:
        return loaders
    with _lock:
        loaders = _skill_loaders_cache.get(user_id)
        if loaders is None:
            loaders = build_skill_loaders(user_id=user_id)
            _skill_loaders_cache[user_id] = loaders
        return loaders


async def assemble_agent(
    state: AgentState,
    user_id: str,
    agent_name: str = _DEFAULT_PROMPT_NAME,
) -> Agent:
    """用外部传入的 state 组装 agent,state 里的 session_id / context / summary
    都是调用方负责灌好的(典型做法:先 load_state 再调本函数)。

    model 全局复用;bash_tool / sandbox 由 UserSandboxManager 按 user_id
    懒管理(每个 user 一份独立容器 + bind mount);skill loaders 按 user_id 缓存。
    Toolkit / Agent 每次新建,因为它们需要绑定本次的 state。

    agent_name 决定走哪份 system_prompt(对应 prompt/prompts/<name>.md);
    默认为 _DEFAULT_PROMPT_NAME(已在模块导入时 fail-fast 校验存在)。
    """
    manager = get_manager()
    # LazyBash:Toolkit 注册期就把 schema/description 填好,真正的 sandbox
    # / docker 容器要等 LLM 真正调 bash 时才拉起(详见 user_sandbox.LazyBash)。
    # 所以这里不再预先 get_or_create + touch——纯聊天的用户不会留下空容器。
    toolkit = Toolkit(
        tools=[LazyBash(manager, user_id), *ALL_TOOLS],
        skills_or_loaders=_get_skill_loaders(user_id),
    )

    # 无条件刷成 DEFAULT:老 session 持久化的 BYPASS mode 不能带进来,
    # 否则 transfer 的 check_permissions(ASK) 在 BYPASS 下会被 fall through 到 ALLOW。
    state.permission_context = PermissionContext(mode=PermissionMode.DEFAULT)

    context_config = ContextConfig(
        trigger_ratio=settings.agent_context_trigger_ratio,
        reserve_ratio=settings.agent_context_reserve_ratio,
    )

    prompt = _DEFAULT_PROMPT if agent_name == _DEFAULT_PROMPT_NAME \
        else load_prompt(agent_name)

    return Agent(
        name=prompt.display_name,
        system_prompt=prompt.content,
        model=_get_model(),
        toolkit=toolkit,
        state=state,
        context_config=context_config,
    )


async def chat_stream(
    state: AgentState,
    user_id: str,
    user_msg: Msg | None = None,
    resume_event=None,
):
    """流式执行 agent,每个事件 yield 一条 str。调用方负责:
        1. 进入前 load_state(...) 拿 state(没有则空 state)
        2. 调本函数(传 user_msg 走新一轮,或传 resume_event 续上次的 ASK)
        3. 流结束后 save_state(...) 落盘

        yield 的内容(每条是一行字符串,自带换行):
          - 工具调用:`{tool_name}  input = '{...}'`
          - 模型最终回答的文本 delta:原始片段
          - ASK 提示:`⚠️ 工具 X 需要您确认\\n  请回复 '确认' 或 '取消'`
        """
    assert (user_msg is None) != (resume_event is None), (
        "chat_stream: exactly one of user_msg / resume_event"
    )

    agent = await assemble_agent(state, user_id=user_id)
    inputs = resume_event if resume_event is not None else user_msg

    # 本次 chat_stream 的 token 累加:agent loop 在一次问答里可能多次调 LLM
    # (工具调用通常 3+ 次),每次结束都来一条 ModelCallEndEvent,这里累加成
    # "一次问答"的总量,在流结束前用 dict 形式 yield 给 router。
    chat_usage = {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    # name_by_id: dict[str, str] = {}
    # input_buf: dict[str, str] = {}
    async for evt_or_msg in agent._reply(inputs=inputs):
        # if isinstance(evt_or_msg, ToolCallStartEvent):
        #     name_by_id[evt_or_msg.tool_call_id] = evt_or_msg.tool_call_name
        #     input_buf[evt_or_msg.tool_call_id] = ""
        # elif isinstance(evt_or_msg, ToolCallDeltaEvent):
        #     input_buf[evt_or_msg.tool_call_id] = (
        #             input_buf.get(evt_or_msg.tool_call_id, "") + evt_or_msg.delta
        #     )
        # elif isinstance(evt_or_msg, ToolCallEndEvent):
        #     cid = evt_or_msg.tool_call_id
        #     name = name_by_id.get(cid, "?")
        #     input_json = input_buf.get(cid, "")
        #     preview = (
        #         input_json if len(input_json) <= 1500
        #         else input_json[:1500] + "...(truncated)"
        #     )
        #     yield f"  {name}  input = {preview!r}\n"
        if isinstance(evt_or_msg, ToolResultTextDeltaEvent):
            # logger.info(evt_or_msg.delta)
            pass
        elif isinstance(evt_or_msg, TextBlockDeltaEvent):
            yield evt_or_msg.delta
        elif isinstance(evt_or_msg, RequireUserConfirmEvent):
            logger.info("chat_stream: got RequireUserConfirmEvent tool_calls=%s",
                        [tc.name for tc in evt_or_msg.tool_calls])
            # 把 ASK 翻成对话文字推给前端,然后流结束——等用户下一句
            # 在 router 那一层被识别成 confirm/deny 再 resume_event 回来。
            # 提示文本优先用工具自己的 confirmation_message(input)
            # (与 check_permissions 的 message 同源——避免两处漂移),
            # 工具没实现就退回通用模板。
            #
            # 包成 <div class="confirm-prompt">,前端 CSS 给红色字体——
            # 走 marked.js + DOMPurify 流水线,div+class 默认是允许的。
            for tc in evt_or_msg.tool_calls:
                try:
                    tool = await agent.toolkit.get_tool(tc.name)
                except Exception:
                    tool = None
                try:
                    tool_input = json.loads(tc.input) if tc.input else {}
                except (ValueError, TypeError):
                    tool_input = {}
                if tool is not None and hasattr(tool, "confirmation_message"):
                    prompt = tool.confirmation_message(tool_input)
                else:
                    prompt = f"⚠️ 工具 {tc.name} 需要您确认"
                # 包成 div.confirm-prompt:前端 renderMessage 对 text 块走
                # renderMarkdown,marked.js 原样放行 HTML,DOMPurify 默认允许
                # div+class,所以历史重渲染时也是同一份红色提示。
                prompt_html = (
                    f'<div class="confirm-prompt">'
                    f'⚠️ {prompt}<br>'
                    f'</div>'
                )
                # 关键:把同一段 HTML 落到 state.context 末尾的 assistant
                # 消息里——这样 openSession / reloadBtn 从 server state 重渲染时
                # 也能看到这条提示(而不是只流期间短暂出现)。
                if state.context and state.context[-1].role == "assistant":
                    state.context[-1].content.append(
                        TextBlock(text=prompt_html),
                    )
                yield f"\n{prompt_html}\n\n"
            yield _MSG_COMPLETE
            # 用 break 而不是 return:让流走到后面的"yield usage",
            # 这样即便触发 ASK 终止本次 chat_stream,也能把"已经发生的"
            # 模型调用 token 累加报给 router,前端能看到"为这一步消耗了
            # 多少 token"。return 会跳过累加 yield,等到下次用户回复确认
            # 走新一轮 chat_stream 才报——把本来属于同一轮的 usage 拆开。
            break
        elif isinstance(evt_or_msg, ModelCallEndEvent):
            logger.info(
                "model call ended: reply_id=%s input=%d output=%d "
                "cache_read=%d cache_write=%d reason=%s",
                evt_or_msg.reply_id,
                evt_or_msg.input_tokens,
                evt_or_msg.output_tokens,
                evt_or_msg.cache_input_tokens,
                evt_or_msg.cache_creation_input_tokens,
                evt_or_msg.finished_reason,
            )
            chat_usage["model_calls"] += 1
            chat_usage["input_tokens"] += evt_or_msg.input_tokens or 0
            chat_usage["output_tokens"] += evt_or_msg.output_tokens or 0
            chat_usage["cache_input_tokens"] += evt_or_msg.cache_input_tokens or 0
            chat_usage["cache_creation_input_tokens"] += (
                evt_or_msg.cache_creation_input_tokens or 0
            )
        elif isinstance(evt_or_msg, _Msg):
            # 这一条 Msg(assistant 回答)已经写到 state.context 末尾了,
            # 发个 checkpoint 给调用方,让它可以立刻落盘。
            yield _MSG_COMPLETE

    # 流结束/中断:把本次 chat_stream 的累计 token 用 dict 报给 router。
    # 用 dict 区分于"普通字符串片段"(router 会把它们当成模型 delta
    # 直接发给前端当 markdown),router 拿到 dict 后单独走 logger +
    # SSE 通道。
    yield {"type": "usage", "data": chat_usage}