# 项目架构设计

## 定位

基于 FastAPI 的 **多用户 AI Agent 服务**：每个用户拥有隔离的 Docker 沙箱，支持工具调用、Skills、上下文压缩、SSE 流式对话、Nacos 服务注册。

技术栈：FastAPI + SQLAlchemy 2.x async + asyncpg + Redis + Nacos SDK + AgentScope 2.0.8 + aiodocker + sse-starlette。

---

## 分层架构（Clean Architecture）

```
┌────────────────────────────────────────────────────────┐
│ main.py                                                │
│   ├─ setup_logger                                      │
│   ├─ load_plugins() ── pkgutil 扫描所有 AppPlugin 子类  │
│   ├─ create_app()  ── middleware / exception handler   │
│   └─ lifespan      ── 顺序执行 plugin.on_startup/shutdown│
└────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────── app/api/v1/ ──────────── 接口层 ─────────────┐
│  agent_router.py   POST /agent/chat (SSE 流)           │
│                    GET  /agent/sessions                │
│                    GET  /agent/sessions/{id}/messages  │
│  user_router.py    用户相关                            │
└────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────── app/domain/ ──────────── 领域层 ─────────────┐
│  model/    SQLAlchemy ORM                             │
│            ├─ chat_session_state                      │
│            └─ sys_user                                │
│  schema/   Pydantic Schema                            │
│  services/                                           │
│    session_service.py  load_state / save_state        │
│                        _auto_deny_orphan_tool_calls   │
└────────────────────────────────────────────────────────┘
            │
            ▼
┌────── app/infrastructure/ ──────── 基础设施层 ──────────┐
│  datasource/                                           │
│    database.py         asyncpg + async_sessionmaker   │
│    redis_client.py     Redis 连接池                   │
│  repositories/         数据访问（Repository 模式）     │
│  agentscope/                                          │
│    agent.py            assemble_agent / chat_stream   │
│    sandbox/                                           │
│      user_sandbox.py   UserSandboxManager            │
│      user_workspace.py SkillSyncedWorkspace           │
│    skill/              public + personal skill        │
│    middware/hostpath_guard   路径越权中间件           │
│    prompt/             system prompt 加载              │
│  tools/                自定义工具（TransferTool）     │
└────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────── app/core/ ──────────── 横切关注点 ───────────┐
│  bootstrap/    插件机制：abs_boot_plugin + 各 *Plugin  │
│  config/       pydantic-settings 读取 .env             │
│  common/       logger                                 │
│  exceptions/   BusinessException + 全局 handler       │
│  security/     repeat_submit_guard 防重复提交         │
│  decorators/   time_elapse 计时装饰器                 │
│  utils/        snowflake / dict_ops / serializer       │
└────────────────────────────────────────────────────────┘
```

---

## 核心机制

### 1. 插件化启动（`AppPlugin` ABC）

`main.py` 通过 `pkgutil.walk_packages` 扫描 `app.core.bootstrap` 下所有 `AppPlugin` 子类，按 `is_enabled()` 决定是否加载，`on_startup()` 依次初始化、`on_shutdown()` 逆序关闭。

| 插件 | 职责 |
|---|---|
| `RouterPlugin` | 自动 `include_router` `app.api.v1.*` |
| `DatabasePlugin` | asyncpg 引擎 + `create_all` |
| `RedisPlugin` | redis 连接池 |
| `NacosPlugin` | 服务注册 + 5s 心跳 |
| `AgentscopePlugin` | 扫孤儿容器、预构建 sandbox 镜像、reaper 定时回收 |
| `FragmentPlugin` | 占位（调试用） |

### 2. 多用户沙箱隔离（`UserSandboxManager`）

模块级单例，每个 `user_id` 对应一份 `UserSandbox = SkillSyncedWorkspace + Bash + last_active_at`：

- `get_or_create`：惰性创建，per-user `asyncio.Lock` 防并发首请求双开
- `sweep_orphans`：启动时清理上次崩溃残留的容器
- `start_reaper` + `reap_idle`：后台任务按 TTL（默认 600s）回收闲置 sandbox
- `_prewarm_image`：后台预构建镜像，首请求走 cache hit

### 3. Agent 流式对话（`agent.py`）

```
router.chat(req)
   ├─ load_state(session, session_id)        # 持久化状态读出
   ├─ _auto_deny_orphan_tool_calls(state)    # 清理上次中断残留 ASKING
   ├─ EventSourceResponse(event_gen):
   │     └─ chat_stream(state, user_id, user_msg) ── 逐事件 yield
   │           ├─ TextBlockDeltaEvent       ──→ 文本片段（SSE）
   │           ├─ RequireUserConfirmEvent   ──→ 转成 HTML 提示 + 落 state
   │           ├─ _Msg                      ──→ _MSG_COMPLETE → checkpoint_save
   │           └─ ModelCallEndEvent         ──→ 累加 token 用量
   └─ save_state + commit + close（in finally）
```

### 4. Skills 双层覆盖

`build_skill_loaders()` = `[LocalSkillLoader(public_dir), PersonalSkillLoader(user_id)]`。Toolkit 按 name 让后者覆盖前者，每个用户一个独立 materialization 目录。

### 5. 状态持久化

`AgentState.model_dump(mode="json")` → JSONB 全量覆盖（`upsert`）。`_auto_deny_orphan_tool_calls` 处理流中断残留的 `ASKING` tool_call，避免 provider 报错 "tool call without result"。

### 6. 危险操作 ASK 机制

`TransferTool` 等 `ToolBase.check_permissions` 返回 `PermissionBehavior.ASK` → 触发 `RequireUserConfirmEvent` → 路由层将用户下一句中文（确认/取消）解析为 `UserConfirmResultEvent` → resume 流。

---

## 关键技术决策

- **StreamingResponse 手动管 session**：`/agent/chat` 不能用 `Depends(get_session)`，因为 Depends 清理早于 SSE 流启动（`agent_router.py` 头部注释解释）
- **进程级单例 model / per-user sandbox**：model 无差异复用，sandbox 必须隔离
- **agentscope 2.0.8 path 补丁**：`agentscope_plugin.py` 顶部 monkey-patch 修复上游 `agentscope.sandbox.*` → `agentscope.workspace.*` 路径错乱
- **重复提交防护**：`repeat_submit_guard`（基于 Redis）
- **Nacos 软依赖**：`nacos_enable=false` 时整个插件跳过

---

## 入口与数据流

```
浏览器（chat.html + marked.js + DOMPurify）
        │
        ▼
POST /api/v1/agent/chat
        │
        ▼
load_state ──→ assemble_agent ──→ chat_stream
                                     │
                                     ▼ (events)
                                    SSE
                                     │
                                     ▼
save_state（每条 Msg 后 checkpoint, finally 兜底）
```

整体走的是「FastAPI + DDD 分层 + 插件化启动 + LLM Agent 流式编排」的组合，每个抽象都对应一个真实痛点：

- 插件化 = 可插拔中间件
- per-user sandbox = 多租户隔离
- 手动 session = StreamingResponse 生命周期
- auto-deny = 流中断恢复
- Repository 模式 = ORM 与领域解耦