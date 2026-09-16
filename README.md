# super-agent

基于 **LangChain deepagents + Alibaba OpenSandbox + PostgreSQL** 的深度智能体。

| 能力 | 实现方式 | 验证状态 |
|---|---|---|
| 多轮对话 | `AsyncPostgresSaver`（checkpointer）按 `thread_id` 持久化完整状态 | ✅ 已测 |
| 长期记忆 | `AsyncPostgresStore`（store）+ 记忆注入 middleware + 记忆管理工具 | ✅ 已测 |
| 沙箱调用 | `OpenSandboxBackend` 适配 deepagents `BaseSandbox`，`execute` + 全套文件工具 | ✅ 已实测 |
| SSE 对话服务 | FastAPI + `StreamingResponse` 流式推送 token/工具/子代理事件 | ✅ 已测 |
| 子代理 | `coder` / `data-analyst` / `researcher` / `reviewer`，`task` 工具派发 | ✅ 已测 |
| 沙箱生命周期 | `SandboxRegistry`：shared/thread 隔离 + 空闲 TTL 回收 | ✅ 已测 |

## 架构

```
                              ┌──────────────────────────────────────┐
                              │           PostgreSQL (Docker)         │
                              │  ├─ checkpoints 表  ← AsyncPostgresSaver（多轮对话）
                              │  └─ store 表       ← AsyncPostgresStore（长期记忆）
                              └───────────────▲──────────────────────┘
                                              │
┌──────────────┐   messages    ┌──────────────┴───────────────────────┐
│   用户 REPL   │ ────────────▶ │        deep agent（deepagents）        │
│  (main.py)   │ ◀──────────── │  ├─ FilesystemMiddleware（文件工具）    │
└──────────────┘   流式回复     │  ├─ SubAgentMiddleware（task 子代理）   │
                              │  ├─ TodoListMiddleware（write_todos）   │
                              │  ├─ MemoryInjectionMiddleware（记忆注入）│
                              │  └─ tools: manage_memory / search_memory│
                              └──────────────┬───────────────────────┘
                                             │ BackendProtocol
                              ┌──────────────▼───────────────────────┐
                              │      OpenSandboxBackend（本项目实现）    │
                              │  原语: execute / upload_files /        │
                              │        download_files / id            │
                              │  派生: ls/read/write/edit/glob/grep    │
                              └──────────────┬───────────────────────┘
                                             │ HTTP (专用事件循环线程)
                              ┌──────────────▼───────────────────────┐
                              │  OpenSandbox server (uvx, Docker runtime)
                              │  └─ 隔离容器: python:3.11-slim + execd │
                              └──────────────────────────────────────┘
```

## 快速开始

### 0. 环境要求

- Python **3.11+**（deepagents 要求 ≥3.11；本项目用 3.13）
- Docker（跑 PostgreSQL 与 OpenSandbox 沙箱容器）
- 一个 LLM API Key（Anthropic / OpenAI / 火山方舟 ARK 等 OpenAI 兼容端点）

### 1. 安装依赖

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv deepagents opensandbox langgraph-checkpoint-postgres langchain-openai "psycopg[binary,pool]" python-dotenv
# 或
uv pip install --python .venv -e .
```

### 2. 启动 PostgreSQL

```bash
docker compose up -d postgres
```

首次运行时程序会自动建表（`setup()` 幂等）。

### 3. 启动 OpenSandbox 服务端

```bash
uvx opensandbox-server init-config sandbox.toml --example docker
# 编辑 sandbox.toml：设置 api_key，把 [store].path 改为 "./.opensandbox/opensandbox.db"
uvx opensandbox-server --config sandbox.toml
```

> 本仓库已含调好的 `sandbox.toml`（`api_key = "local-dev-key"`，Docker runtime）。

> **首次创建沙箱较慢**：服务端第一次要构建 execd 缓存（约数分钟），
> 可能超过 SDK 默认 30s 请求超时；`OpenSandboxBackend.create()` 已将
> `request_timeout` 默认放宽到 120s，如仍超时可预先 `docker pull` 沙箱镜像
> 或进一步调大该参数。

### 4. 配置模型与环境

```bash
copy .env.example .env
# 编辑 .env：填入 MODEL 与对应 API Key
```

常用组合：

| 提供方 | MODEL | 需要的环境变量 |
|---|---|---|
| **本地 LM Studio（推荐）** | `qwen3-4b-finetuned`（仅模型名） | `OPENAI_BASE_URL=http://localhost:1234/v1` + `OPENAI_API_KEY=lm-studio` |
| Anthropic | `anthropic:claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai:gpt-4o` | `OPENAI_API_KEY` |
| 火山方舟 ARK | `doubao-seed-1-6`（仅模型名） | `OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3` + `OPENAI_API_KEY` |
| DeepSeek | `deepseek-chat`（仅模型名） | `OPENAI_BASE_URL=https://api.deepseek.com/v1` + `OPENAI_API_KEY` |

**本地 LM Studio 全离线接入**：先在 LM Studio 中加载一个 chat 模型和一个 embedding 模型，然后在 `.env` 中配置：

```env
MODEL=qwen3-4b-finetuned            # LM Studio 中已加载的 chat 模型名
OPENAI_BASE_URL=http://localhost:1234/v1   # LM Studio 本地服务
OPENAI_API_KEY=lm-studio

EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b  # embedding 模型名
EMBEDDING_BASE_URL=http://localhost:1234/v1
EMBEDDING_API_KEY=lm-studio
```

设置 `EMBEDDING_MODEL` 即启用长期记忆**语义检索**（本项目用本地 embedding 模型，维度 1024 已适配）；未设置时退化为按更新时间检索。

### 5. 运行

```bash
.venv\Scripts\python main.py
```

会话内命令：

| 命令 | 作用 |
|---|---|
| `/new` | 开新会话（新 thread，短期记忆清空，长期记忆保留） |
| `/memories` | 查看当前用户全部长期记忆 |
| `/exit` | 退出（自动销毁沙箱） |

试一试：

```
你> 我叫小明，是一名后端工程师，喜欢用 Python。请记住这些信息。
你> /new
你> 还记得我是谁吗？我的技术栈是什么？        ← 长期记忆跨会话生效
你> 用沙箱算一下 2 的 100 次方，并把结果写到 /tmp/result.txt
```

### 6. 启动 FastAPI + SSE 对话服务

除 CLI REPL 外，项目还提供基于 **FastAPI + SSE** 的 Web 服务（同名 `build_agent` 组装逻辑，多用户/多会话并发）：

```bash
.venv\Scripts\python server.py
# 或等价：.venv\Scripts\uvicorn server:app --host 127.0.0.1 --port 8000
```

打开 **http://127.0.0.1:8000** 即是一个聊天地demo页（`static/index.html`），支持流式打字、子代理事件展示、会话历史与记忆管理。

> Windows 注意：uvicorn 默认用 `ProactorEventLoop`，与 psycopg 异步驱动不兼容；`server.py` 已显式覆盖为 `SelectorEventLoop`。

#### API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 聊天地emo页 |
| `GET` | `/api/health` | 健康检查（model + sandbox_mode） |
| `GET` | `/api/agents` | 主 agent + 全部子代理清单 |
| `POST` | `/api/chat` | **SSE 流式对话**（核心） |
| `GET` | `/api/threads/{id}/history` | 会话历史（从 checkpoint 恢复） |
| `DELETE` | `/api/threads/{id}/sandbox` | 立即销毁该会话沙箱 |
| `GET` | `/api/memories` | 列出/检索长期记忆 |
| `POST` | `/api/memories` | 写入一条长期记忆 |
| `DELETE` | `/api/memories/{key}` | 删除一条记忆 |

`POST /api/chat` 请求体：

```jsonc
{ "message": "写个二分查找", "thread_id": "可选，缺省新建", "user_id": "可选，记忆归属" }
```

SSE 事件类型（`event:` 行 + `data:` JSON）：

| 事件 | 载荷要点 |
|---|---|
| `start` | `thread_id`、`user_id` |
| `token` | `content` 文本增量 |
| `tool_start` / `tool_end` | 工具名 + 入参 / 返回预览 |
| `subagent_start` / `subagent_end` | 子代理名 + 派发描述 / 回报预览 |
| `memory` | 长期记忆写入 |
| `done` | 本轮完整回复 |
| `error` | 错误信息 |

#### 沙箱注册表（`app/api/sandbox_registry.py`）

- **shared 模式**：所有会话共用一个沙箱（省资源，默认）。
- **thread 模式**：每个 `thread_id` 独享一个沙箱（状态隔离），由 `OPENSANDBOX_TIMEOUT` 控制空闲秒数，后台协程周期回收；接口也支持 `DELETE /api/threads/{id}/sandbox` 手动销毁。
- 每次请求结束释放引用（`release`），空闲沙箱由 TTL 回收协程统一清理。

## 三大能力实现详解

### 1. 多轮对话（`app/db.py`）

`AsyncPostgresSaver.from_conn_string()` 建立 checkpointer，传给
`create_deep_agent(checkpointer=...)`。每个 `thread_id` 一条完整状态
（消息、todos、文件 state），进程重启后继续对话：

```python
async with postgres_persistence() as (checkpointer, store):
    agent = build_agent(backend=backend, checkpointer=checkpointer, store=store)
    config = {"configurable": {"thread_id": "t1"}}
    await agent.ainvoke({"messages": ("user", "你好")}, config=config)   # 第 1 轮
    await agent.ainvoke({"messages": ("user", "继续")}, config=config)   # 第 2 轮，自动带历史
```

### 2. 长期记忆（`app/memory.py`）

三层设计，全部落在 PostgreSQL `store` 表，namespace 为 `("memories", user_id)`：

- **写入**：模型调用 `manage_memory` 工具（hot path，模型自主判断什么值得记）
- **检索**：`MemoryInjectionMiddleware` 在每次模型调用前，用最近一条用户消息做
  语义检索（配置 embedding 时）或按时间取最近条目，注入 system prompt；
  注入发生在 `wrap_model_call`，**不污染对话历史**
- **查询**：模型可主动调 `search_memory` 工具精确回忆

跨 thread、跨进程共享：新会话 `/new` 后依然记得用户是谁。

### 3. 沙箱后端（`app/sandbox_backend.py`，核心交付物）

deepagents 的 `BaseSandbox` 只要求实现 4 个原语，其余文件工具全部自动派生：

| 原语 | OpenSandbox 对应实现 |
|---|---|
| `execute(command, timeout)` | `sandbox.commands.run(cmd, opts=RunCommandOpts(timeout=...))` |
| `upload_files(files)` | 逐文件 `create_directories` + `write_file`（契约：保证父目录、部分成功） |
| `download_files(paths)` | 逐文件 `read_bytes`（契约：部分成功，错误进 `error` 字段） |
| `id` | `sandbox.id` |

关键工程点：OpenSandbox SDK 是 asyncio 原生的，而 deepagents 的同步协议方法
可能被 `asyncio.to_thread` 调到任意线程；httpx `AsyncClient` 不能跨事件循环。
因此 `OpenSandboxBackend` 内部维护**一个专用事件循环线程**，沙箱的全部
创建/操作/销毁固定在该循环上，同步/异步调用统一经 `run_coroutine_threadsafe`
桥接——从根源上避免跨循环崩溃。

## 项目结构

```
super_agent/
├── main.py                    # CLI 入口（REPL，流式输出）
├── server.py                  # FastAPI + SSE 服务入口
├── static/index.html          # SSE 聊天地emo页
├── app/
│   ├── __init__.py            # Windows 事件循环策略修正
│   ├── config.py              # 环境变量配置（.env）
│   ├── db.py                  # PostgreSQL 持久化（checkpointer + store）
│   ├── memory.py              # 长期记忆：注入 middleware + 记忆工具
│   ├── sandbox_backend.py     # OpenSandbox → deepagents 沙箱后端适配器
│   ├── agent.py               # deep agent 组装（模型/工具/子代理/中间件）
│   └── api/
│       ├── routes.py          # FastAPI 路由 + SSE 事件翻译
│       ├── schemas.py         # API 请求/响应模型
│       ├── sandbox_registry.py# 沙箱生命周期（shared/thread + TTL 回收）
│       └── __init__.py
├── tests/
│   ├── test_imports.py        # 导入冒烟测试
│   ├── test_sandbox_backend.py# 沙箱后端单元测试（FakeSandbox）
│   ├── test_memory_flow.py    # 记忆链路集成测试（真实 PostgreSQL）
│   ├── test_real_sandbox.py   # 真实 OpenSandbox 端到端测试
│   └── test_api.py            # FastAPI + SSE 集成测试（fake 模型 + 真实 PG）
├── docker-compose.yml         # PostgreSQL（OpenSandbox server 可选）
├── sandbox.toml               # OpenSandbox 服务端配置（Docker runtime）
├── pyproject.toml
└── .env.example
```

## 运行测试

```bash
.venv\Scripts\python -m tests.test_imports          # 无外部依赖
.venv\Scripts\python -m tests.test_sandbox_backend  # 无外部依赖
.venv\Scripts\python -m tests.test_memory_flow      # 需 PostgreSQL
.venv\Scripts\python -m tests.test_api              # 需 PostgreSQL
.venv\Scripts\python -m tests.test_real_sandbox     # 需 opensandbox-server + Docker
```

测试覆盖：BaseSandbox 抽象契约（部分成功、父目录创建、timeout 透传）、
同步/异步桥接、多轮对话历史保持、长期记忆写入/注入/跨 thread 共享、
真实沙箱 execute 与派生文件工具（write/read/edit/ls/glob/grep），以及
FastAPI/SSE：健康检查、子代理清单、SSE 事件流（token/tool/memory/subagent）、
会话历史恢复、404 处理、长期记忆 CRUD。

## 配置参考

见 `.env.example` 内注释。全部环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `POSTGRES_*` / `DATABASE_URL` | localhost:5432 | PostgreSQL 连接 |
| `MODEL` | `anthropic:claude-sonnet-4-6` | `provider:model` 或（配 BASE_URL 时）纯模型名 |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | - | OpenAI 兼容端点 |
| `EMBEDDING_MODEL` 等 | - | 长期记忆语义检索（可选） |
| `OPENSANDBOX_DOMAIN` | `localhost:8080` | OpenSandbox 服务端地址 |
| `OPENSANDBOX_API_KEY` | `local-dev-key` | 服务端 API Key |
| `OPENSANDBOX_IMAGE` | `python:3.11-slim` | 沙箱镜像 |
| `OPENSANDBOX_TIMEOUT` | - | 沙箱空闲回收秒数 |
| `USER_ID` | `demo_user` | 长期记忆归属用户 |
| `MEMORY_TOP_K` | `5` | 每轮注入的记忆条数 |

## 主要参考

- deepagents：<https://github.com/langchain-ai/deepagents>（文档：<https://docs.langchain.com/oss/python/deepagents/overview>）
- OpenSandbox：<https://github.com/opensandbox-group/OpenSandbox>（官网：<https://open-sandbox.ai/>）
- LangGraph Persistence / Memory：<https://docs.langchain.com/oss/python/langgraph/persistence>
