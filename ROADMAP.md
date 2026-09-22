# AgentForge 三阶段执行计划书

> 基于可行性分析重构：**端到端先打通再补深度**，避免单任务过载，每个版本独立交付可用产品。

---

## 总体版本规划

| 版本 | 定位 | 核心闭环 | 部署形态 | 周期（单人+AI） |
|---|---|---|---|---|
| **V1 MVP** | 端到端打通 | 注册→上传 SKILL.md→对话调用 Agent→SSE 看结果→查历史 | 单体 + Docker Compose | 6-8 周 |
| **V2 产品化** | 完整可视化平台 | 画布拖拽编排→编译→部署→子代理→评测→可观测 | 模块化单体 + Redis | 8-12 周 |
| **V2.5 生产化** | 补齐 V2 能力缺口 | 条件分支+HIL+并行 / RAG / MCP / 配额审计 / K8s | 模块化单体 + K8s | 6-8 周 |
| **V3 企业化** | 微服务 + 协议演进 | 多服务集群→A2A 互通→模板市场→SSO | K8s + Nacos | 10-14 周 |

**总周期约 8-12 个月**，每个版本结束即可对外交付。

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

# V2.5 · 生产化补强（V2 → V3 过渡）

> **定位**：不拆微服务（留 V3），补齐 V2 暴露的 5 大能力缺口，从「演示级」→「可生产级」。
> **原则**：编排补强优先（workflow engine 硬伤），RAG + MCP + 治理三线并进，K8s 收尾。

## 差距诊断（V2 验收后）

| # | 差距维度 | V2 现状 | 主流平台基线 |
|---|---|---|---|
| 1 | 编排能力 | sequential only | 条件分支 + 并行 + HIL + 循环 |
| 2 | 生态集成 | OpenSandbox 独家 | 100+ 工具 + MCP 协议 |
| 3 | 企业治理 | 租户隔离 + JWT | 配额 + 审计 + SSO + 脱敏 |
| 4 | RAG / 知识库 | 长期记忆 only | 文档解析 + 向量检索 + 引用 |
| 5 | 部署形态 | Docker Compose | K8s + 水平扩容 + 全链路监控 |

## 功能边界

**做**：条件分支 + 人机协同 + 并行子代理、RAG 基础能力、MCP 协议、配额/审计/灰度、K8s Helm + OTel 全链路。

**不做**（留 V3）：微服务拆分、A2A/ACP 协议、adversarial 子代理、模板市场、SSO。

## 任务分解

| 任务 | 目标 | 周期 | 依赖 | 优先级 |
|---|---|---|---|---|
| **V2.5-T1** 条件分支节点 | `if`/`switch` 节点 + 表达式求值器 + 编译器扩展 + 前端节点面板 | 1 周 | V2-T2 | 高 |
| **V2.5-T2** 人机协同（HIL） | `interrupt()` + 恢复 + 审批节点 + 前端审批 UI（挂起→人工确认→续跑） | 1 周 | V2-T5 | 高 |
| **V2.5-T3** 并行子代理 | `fan-out`/`fan-in` + 结果合并策略（first/all/merge） | 5 天 | V2-T4 | 中 |
| **V2.5-T4** RAG 对接接口预留 | 定义 RAG service 接口契约（query→contexts+citation）+ stub 实现 + 配置项指向外部 LightRAG 服务地址 | 2 天 | V2-T11 | 高 |
| **V2.5-T5** RAG 工具注入 + 引用渲染 | Agent 工具 `retrieve_knowledge` 调用外部 RAG 服务 + 前端 citation 渲染 + 联调开关（stub/真实） | 3 天 | T4 | 中 |
| **V2.5-T6** MCP 协议支持 | MCP client + 工具自动发现 + 安全沙箱（限定工具能力域） | 1 周 | — | 高 |
| **V2.5-T7** 工具市场骨架 | 插件 manifest + 注册中心 + 一键安装 + 权限校验 | 5 天 | T6 | 中 |
| **V2.5-T8** 配额与限流 | QPS / Token / 调用次数 + 租户级配额 + Redis 令牌桶 | 5 天 | V2 多租户 | 高 |
| **V2.5-T9** 审计日志 | 操作审计 + 数据访问审计 + 审计查询 API + 保留策略 | 5 天 | T8 | 中 |
| **V2.5-T10** API Key 轮转 + 灰度发布 | Key 生命周期（创建/吊销/轮转） + Workflow 版本灰度（流量切分） | 5 天 | T8 | 中 |
| **V2.5-T11** K8s Helm chart + 水平扩容 | Helm chart + HPA + 配置外部化（ConfigMap/Secret） + PV 持久化 | 1 周 | V2 Docker | 高 |
| **V2.5-T12** OTel 全链路可观测 + 告警 | OTLP exporter + Prometheus + Grafana dashboard + 告警规则 | 5 天 | T11 | 中 |
| **V2.5-T13** E2E 联调 | 条件分支→HIL→RAG→MCP→配额→审计全链路 | 5 天 | 全部 | 高 |

## V2.5 验收

- ✅ 画布拖出 `start → if(条件) → agent_A / agent_B → merge → end`，按条件分流
- ✅ 审批节点：Agent 产出方案 → 挂起 → 人工审批 → 通过则续跑，拒绝则走 fallback 分支
- ✅ 并行子代理：3 个 agent 并行执行，fan-in 取 first/all/merge
- ✅ RAG 接口契约定义完成，stub 实现可返回模拟上下文 + citation
- ✅ Agent 通过 `retrieve_knowledge` 工具调用外部 RAG 服务，前端渲染引用来源
- ✅ 联调开关：`RAG_MODE=stub|live` 切换 stub/真实 LightRAG 服务（真实服务后续联调）
- ✅ 接入 MCP server，自动发现工具并注册为 Agent 可用工具
- ✅ 工具市场：安装插件 → manifest 校验 → 权限确认 → 工具可用
- ✅ 租户配额：超过 QPS/Token 上限返回 429，审计日志记录全部关键操作
- ✅ API Key 轮转：旧 Key 吊销后调用失效，新 Key 立即生效
- ✅ Workflow 灰度：v2 上线 10% 流量，可一键回滚到 v1
- ✅ `helm install` 一键起集群，HPA 自动扩容 backend 副本
- ✅ Grafana 看板：trace + token 用量 + 错误率 + 告警通知

## V2.5 关键设计决策

- **条件分支用表达式求值器**（如 `eval_ex`），不引入完整 DSL，保持编译器简单
- **HIL 复用 LangGraph `interrupt()`**，不自研挂起/恢复机制
- **RAG 外置为独立服务**：本项目不集成 LightRAG/Milvus/MinerU（后续自建独立部署），仅预留 RAG service 接口契约（query→contexts+citation）+ stub 实现 + `RAG_MODE=stub|live` 联调开关。真实 LightRAG 服务后续联调
- **MCP 优先于 A2A**：MCP 已有参考实现，A2A spec 仍在演进（留 V3）
- **配额用 Redis 令牌桶**，不用数据库计数（性能 + 实时性）
- **K8s 优先于微服务拆分**：先解决部署可生产性，再解决架构可扩展性

## V2.5 任务依赖图

```
                    ┌── T1 条件分支 ──┐
                    │                 ├── T3 并行 ──┐
V2-T2 编译器 ───────┤                 │             │
                    └── T2 HIL ───────┘             │
                                                     │
V2-T11 记忆 ──────── T4 RAG 接口预留 ─── T5 RAG 工具+引用 ─┤
                                                     ├── T13 E2E 联调
                     T6 MCP ────────── T7 工具市场 ──┤
                                                     │
V2 多租户 ────────── T8 配额限流 ──── T9 审计 ───────┤
                          │                          │
                          └── T10 Key 轮转+灰度 ─────┤
                                                     │
V2 Docker ─────────── T11 K8s Helm ──── T12 OTel ───┘
```

## V2.5 测试目标

- 单元测试：表达式求值 / interrupt 状态机 / fan-in 合并 / RAG 分块 / MCP client
- 集成测试：条件分支 E2E / HIL 审批流 / RAG 全链路 / 配额限流 / 灰度切流
- 测试结果目标：200+ passed（V2 基线 170 + V2.5 新增 30+）

---

# V3 · 企业化（微服务 + 协议演进）

**原则**：单租户→多租户集群，单体→微服务，闭环→开放协议互通。

## 功能边界

**做**：数据库垂直拆分、Nacos 服务注册、服务抽取（agent-runtime / sandbox / workflow / api 四服务）、A2A Registry + Gateway、模板市场、adversarial 子代理、SSO。

**可选做**：ACP 协议（spec 成熟后再说）。

> 注：V2.5 已覆盖灰度发布、RAG 基础、安全治理、K8s 部署；V3 聚焦微服务化 + A2A 协议 + 模板市场。

## 任务分解

| 任务 | 目标 | 周期 | 风险 |
|---|---|---|---|
| **V3-T1** 数据库垂直拆分 | agent_db / sandbox_db / workflow_db / shared_db | 2 周 | 中（跨库事务） |
| **V3-T2** Nacos 接入 | 服务注册 + 配置中心 + 动态路由 | 1 周 | 低 |
| **V3-T3** 服务抽取 | 拆 agent-runtime / sandbox-manager / workflow-service / api-gateway | 3 周 | 高（依赖梳理） |
| **V3-T4** A2A Registry | Agent 卡片注册 + 发现 + 能力声明 | 2 周 | 高（协议新） |
| **V3-T5** A2A Gateway | 跨 Agent 任务派发 + 状态同步 | 2 周 | 高 |
| **V3-T6** 模板市场 | Workflow/Skill/Prompt 上架 + 评分 + 一键安装 | 2 周 | 中 |
| **V3-T7** adversarial 子代理 | 引入 judge agent + 投票合并 | 1 周 | 中 |
| **V3-T8** SSO + 数据脱敏 | OAuth2/SAML SSO + PII 检测/脱敏 | 2 周 | 中 |
| **V3-T9** ACP 协议（可选） | Agent 通信协议适配层 | 2 周 | 极高（spec 未稳定） |
| **V3-T10** 多集群运维手册 | 多集群部署 + 跨集群灰度 + 运维 runbook | 1 周 | 低 |

> 注：V2.5 已覆盖单集群 K8s、灰度发布、RAG、安全治理基础；V3 聚焦微服务化 + A2A + 模板市场 + SSO。

## V3 验收

- 4 个微服务独立部署，Nacos 注册可见
- A2A Registry 注册 3 个外部 Agent，主 Agent 可派发任务并回收结果
- 模板市场可上架 Skill，其他租户可一键安装
- SSO 登录支持 OAuth2，PII 字段自动脱敏
- 多集群灰度：v2 在 cluster-A 上线，可一键切流到 cluster-B

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
