# AgentForge 三阶段执行计划书

> 基于可行性分析重构：**端到端先打通再补深度**，避免单任务过载，每个版本独立交付可用产品。

---

## 总体版本规划

| 版本 | 定位 | 核心闭环 | 部署形态 | 周期（单人+AI） |
|---|---|---|---|---|
| **V1 MVP** | 端到端打通 | 注册→上传 SKILL.md→对话调用 Agent→SSE 看结果→查历史 | 单体 + Docker Compose | 6-8 周 |
| **V2 产品化** | 完整可视化平台 | 画布拖拽编排→编译→部署→子代理→评测→可观测 | 模块化单体 + Redis | 8-12 周 |
| **V3 企业化** | 微服务 + 协议演进 | 多服务集群→A2A 互通→灰度/RAG/治理→模板市场 | K8s + Nacos | 12-16 周 |

**总周期约 6-9 个月**，每个版本结束即可对外交付。

---

# V1 · MVP 端到端打通

**原则**：能用代码配置就不用画布，能跑通就不用复杂编排，能复用 super-agent 就不自研。

## 功能边界

**做**：多租户认证、SKILL.md 文件加载、Agent 单轮/多轮对话、OpenSandbox 集成、SSE 流式、会话历史、Docker Compose 一键起。

**不做**（留 V2）：Workflow 画布、子代理编排、模型路由、检查点回退、评测面板、可观测性指标。

## 任务分解

| 任务 | 目标 | 周期 | 复用 |
|---|---|---|---|
| **V1-T1** 骨架 | FastAPI + async SQLAlchemy + 配置 | 2 天 | 参考 `app/config.py` |
| **V1-T2** 数据层 | Tenant/User/Skill/Session/Message 五张表，**无 RLS**（用 `tenant_id` where 过滤） | 2 天 | 参考 `app/db.py` |
| **V1-T3** 租户认证 | JWT + ContextVar + 注册/登录/me | 2 天 | — |
| **V1-T4** Skill 加载 | 目录扫描 + frontmatter 解析 + 按 tenant_id 隔离目录 | 2 天 | — |
| **V1-T5** Agent 运行时（基础） | `create_deep_agent` + 单一 system_prompt + tools=空 + backend | 3 天 | 参考 `app/agent.py` 的 `build_agent` |
| **V1-T6** OpenSandbox 集成 | 直接迁移 `OpenSandboxBackend` + shared 模式（不分租户沙箱） | 1 天 | **直接搬** `app/sandbox_backend.py` |
| **V1-T7** SSE 对话 API | `POST /api/v1/chat` 流式 + 事件翻译 | 3 天 | 参考 `app/api/routes.py` |
| **V1-T8** 会话历史 | 按 session_id 取消息列表（从 messages 表读，不用 checkpoint） | 1 天 | — |
| **V1-T9** 最小前端 | 单页 chat UI（流式打字 + skill 列表 + 历史抽屉） | 3 天 | 参考 `static/index.html` |
| **V1-T10** Docker Compose | postgres + backend + 前端 nginx | 1 天 | 参考 `docker-compose.yml` |
| **V1-T11** 端到端冒烟 | 注册→上传 SKILL.md→对话→查历史 | 2 天 | — |

## V1 验收

- 三租户并行，A 看不到 B 的会话和 Skill
- 上传 `SKILL.md` 后 Agent 能识别并使用该能力
- 对话 SSE 流式输出 token + tool 调用摘要
- `/new` 开新会话，历史会话可恢复
- `docker compose up` 一键起，访问 80 端口即用

## V1 风险与对策

- **沙箱资源隔离**：V1 用 shared 模式（所有租户共用一个沙箱），用 `~/tenant_{id}/` 前缀做文件隔离；V2 再切 thread 模式。
- **deepagents API 不稳**：V1 不用 subagents / interrupt / memory middleware，只传 `model + system_prompt + tools + backend + checkpointer`，最稳定的子集。

---

# V2 · 产品化（完整可视化平台）

**原则**：补齐 V1 跳过的可视化、编排、治理能力，但仍是模块化单体。

> **状态：✅ 已完成** — 全部 12 个任务（T1-T12）开发 + 单测 + E2E 联调通过（170 passed）。

## 功能边界

**做**：Workflow 画布（React Flow）+ 编译器、子代理 sequential 编排、检查点、模型路由、评测面板、OpenTelemetry 可观测、thread 模式沙箱、Prompt 版本管理。

**不做**（留 V3）：微服务拆分、A2A/ACP、灰度发布、Agentic RAG、模板市场、adversarial 子代理。

## 任务分解

| 任务 | 目标 | 周期 | 依赖 | 状态 |
|---|---|---|---|---|
| **V2-T1** Workflow 数据模型 | `WorkflowDefinition` JSONB + nodes/edges CRUD | 3 天 | V1 | ✅ |
| **V2-T2** 编译器 | 拓扑排序 + 环检测 + 节点合并为 deepagents 配置 | 1 周 | T1 | ✅ |
| **V2-T3** 前端画布 | Vue Flow + 节点面板 + 连线校验 + 导出 JSON | 2 周 | T1 | ✅ |
| **V2-T4** 子代理（sequential） | `task` 工具 + SubagentOrchestrator（仅串行） | 1 周 | T2 | ✅ |
| **V2-T5** 检查点 | AsyncPostgresSaver + create/restore/list API | 5 天 | V1-T2 升级 | ✅ |
| **V2-T6** 模型路由 | auto/cost/quality 四策略 + 任务类型映射 | 3 天 | — | ✅ |
| **V2-T7** thread 模式沙箱 | SandboxRegistry + TTL 回收 | 3 天 | 直接搬 `app/api/sandbox_registry.py` | ✅ |
| **V2-T8** 评测面板 | TestCase + golden + 运行 + 通过率 | 1 周 | — | ✅ |
| **V2-T9** 可观测性 | OTel trace + 用量统计 + trace 列表页 | 1 周 | — | ✅ |
| **V2-T10** Prompt 版本管理 | Prompt 表 + 版本 diff + 回滚 | 5 天 | — | ✅ |
| **V2-T11** 长期记忆（可选） | AsyncPostgresStore + MemoryInjectionMiddleware | 3 天 | 直接搬 `app/memory.py` | ✅ |
| **V2-T12** 联调 + E2E | 画布→编译→部署→子代理→检查点→评测 全链路 | 1 周 | 全部 | ✅ |

## V2 验收

- ✅ 用户在画布拖出 start→subagent→end，点击部署即可对话
- ✅ 子代理 sequential 串行执行，结果回流主 Agent
- ✅ 创建检查点 → 改 prompt → 回退 → 状态恢复
- ✅ 评测面板跑 golden case，显示通过率（含 contains/regex/similarity 三种断言）
- ✅ 对话生成 trace，可在 API 查看（span + token 用量 + 耗时统计）
- ✅ Prompt 版本管理：版本自增 + active 切换 + 版本 diff
- ✅ 长期记忆：MemoryInjectionMiddleware 自动注入 + manage_memory/search_memory 工具
- ✅ 多租户隔离：workflow / test / trace / prompt 全部按 tenant_id where 过滤
- ✅ E2E 全链路测试：`tests/test_v2_e2e.py` 覆盖画布→编译→部署→子代理→检查点→评测→可观测→Prompt→多租户隔离

## V2 测试覆盖

| 测试文件 | 覆盖范围 | 依赖 |
|---|---|---|
| `test_v2_workflow.py` | 编译器 + 子代理编排 + Workflow/Version/Checkpoint 模型（SQLite） | 无 |
| `test_v2_eval_trace_prompt.py` | 评测断言/批量运行 + TraceCollector + Prompt 版本管理（SQLite） | 无 |
| `test_v2_memory.py` | 长期记忆 namespace + tools + middleware（FakeStore） | 无 |
| `test_v2_sandbox_registry.py` | SandboxRegistry shared/thread 隔离 + TTL 回收 | 无 |
| `test_v2_api.py` | Workflow CRUD + 编译 + 编排 + 多租户隔离（真实 PG） | PG + Agent 栈 |
| `test_v2_e2e.py` | 全链路 E2E：画布→编译→部署→子代理→检查点→评测→可观测→Prompt | PG + Agent 栈 |

**测试结果**：170 passed（PG 在线）/ 161 passed + 9 skipped（无 PG 时集成测试自动跳过）

## V2 关键减负决策

- **画布用 React Flow**，不自绘 SVG，省 2-3 周
- **子代理只做 sequential**，adversarial/parallel 留 V3
- **检查点用 LangGraph 自带** `AsyncPostgresSaver`，不自研
- **沙箱生命周期直接复用** super-agent 的 `SandboxRegistry`

---

# V3 · 企业化（微服务 + 协议演进）

**原则**：单租户→多租户集群，单体→微服务，闭环→开放协议互通。

## 功能边界

**做**：数据库垂直拆分、Nacos 服务注册、服务抽取（agent-runtime / sandbox / workflow / api 四服务）、A2A Registry + Gateway、灰度发布、Agentic RAG、安全治理、模板市场。

**可选做**：ACP 协议（spec 成熟后再说）、adversarial 子代理、parallel 子代理。

## 任务分解

| 任务 | 目标 | 周期 | 风险 |
|---|---|---|---|
| **V3-T1** 数据库垂直拆分 | agent_db / sandbox_db / workflow_db / shared_db | 2 周 | 中（跨库事务） |
| **V3-T2** Nacos 接入 | 服务注册 + 配置中心 + 动态路由 | 1 周 | 低 |
| **V3-T3** 服务抽取 | 拆 agent-runtime / sandbox-manager / workflow-service / api-gateway | 3 周 | 高（依赖梳理） |
| **V3-T4** A2A Registry | Agent 卡片注册 + 发现 + 能力声明 | 2 周 | 高（协议新） |
| **V3-T5** A2A Gateway | 跨 Agent 任务派发 + 状态同步 | 2 周 | 高 |
| **V3-T6** 灰度发布 | Workflow/Agent 版本灰度 + 流量切分 | 1 周 | 中 |
| **V3-T7** Agentic RAG | 向量库 + 检索工具 + 注入策略 | 2 周 | 中 |
| **V3-T8** 安全治理 | API Key 轮转 + 配额 + 审计 + 数据脱敏 | 2 周 | 中 |
| **V3-T9** 模板市场 | Workflow/Skill/Prompt 上架 + 评分 + 一键安装 | 2 周 | 中 |
| **V3-T10** adversarial 子代理 | 引入 judge agent + 投票合并 | 1 周 | 中 |
| **V3-T11** ACP 协议（可选） | Agent 通信协议适配层 | 2 周 | 极高（spec 未稳定） |
| **V3-T12** K8s 部署 + 文档 | Helm chart + 运维手册 | 1 周 | 低 |

## V3 验收

- 4 个微服务独立部署，Nacos 注册可见
- A2A Registry 注册 3 个外部 Agent，主 Agent 可派发任务并回收结果
- Workflow 灰度：v2 上线 10% 流量，可一键回滚
- 模板市场可上架 Skill，其他租户可一键安装
- K8s `helm install` 一键起集群

## V3 风险与对策

- **A2A 协议新**：先实现 `agent.json` 卡片 + JSON-RPC task 派发的最小子集，不追求 spec 全覆盖。
- **微服务拆分依赖混乱**：先用 V2 的模块边界（modules/）作为拆分蓝图，避免重设计。
- **ACP 视协议成熟度决定是否做**：V3 末评估，不成熟就跳过，不影响交付。

---

# 跨版本的关键复用清单

| V 版本任务 | 直接复用 super-agent 现有代码 |
|---|---|
| V1-T5 Agent 运行时 | `app/agent.py` `build_agent` 简化版 |
| V1-T6 OpenSandbox | `app/sandbox_backend.py` 整体搬 |
| V1-T7 SSE 对话 | `app/api/routes.py` 翻译层 |
| V2-T5 检查点 | `app/db.py` `AsyncPostgresSaver` |
| V2-T7 沙箱生命周期 | `app/api/sandbox_registry.py` |
| V2-T11 长期记忆 | `app/memory.py` |
| V1-T9 前端 | `static/index.html` 升级 |

---

# 执行建议

1. **V1 先跑通**：不要追求画布和编排，让用户能注册→传 SKILL→对话→查历史就上线。MVP 验证产品方向比功能堆砌重要。

2. **V2 关键路径**：画布（V2-T3）+ 编译器（V2-T2）+ 子代理（V2-T4）是产品价值核心，也是工作量主体。React Flow 选型是 V2 最大的减负决策。

3. **V3 协议先行评估**：A2A/ACP 启动前先花 3 天做 spec 调研，确认有参考实现再投入，否则降级为"内部 Agent 互调"。

4. **每版本预留 20% 返工 buffer**：V2 跑通后必回头改 V1 的契约；V3 拆服务必暴露 V2 的模块边界问题。

5. **测试驱动多租户隔离**：从 V1-T2 起就写"租户 A 写→B 查不到"的自动化测试，每个版本回归。

---

## 三版本对比总结

| 维度 | V1 MVP | V2 产品化 | V3 企业化 |
|---|---|---|---|
| 编排方式 | 代码配置 | 画布拖拽 | 画布 + A2A 跨 Agent |
| 子代理 | 无 | sequential | sequential + adversarial |
| 沙箱 | shared | thread 隔离 | 多服务独立沙箱池 |
| 部署 | Docker Compose | Docker Compose + Redis | K8s + Nacos |
| 周期 | 6-8 周 | 8-12 周 | 12-16 周 |
| 风险 | 低 | 中（画布+编译器） | 高（微服务+协议新） |
