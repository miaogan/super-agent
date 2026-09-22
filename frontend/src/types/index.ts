// 后端 API 类型定义（与 backend/app/api/schemas.py 对齐）

export interface TenantContext {
  tenant_id: string
  user_id: string
  email: string
  display_name?: string | null
}

export interface RegisterRequest {
  name: string
  email: string
  password: string
}

export interface RegisterResponse {
  tenant_id: string
  api_key: string
  user_id: string
  email: string
}

export interface LoginRequest {
  email: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  tenant_id: string
  user_id: string
  email: string
}

export interface AgentInfo {
  name: string
  description: string
  system_prompt: string
}

export interface AgentsResponse {
  main_agent: AgentInfo
  subagents: AgentInfo[]
}

export interface SkillItem {
  name: string
  description: string
  content: string
  is_global: boolean
  tenant_id?: string | null
}

export interface SkillListResponse {
  items: SkillItem[]
}

export interface SkillCreateRequest {
  name: string
  description: string
  content: string
}

export interface MemoryItem {
  key: string
  text: string
  created_at?: string | null
}

export interface MemoriesResponse {
  user_id: string
  items: MemoryItem[]
}

export interface SessionItem {
  id: string
  thread_id: string
  title?: string | null
  created_at: string
  last_active_at: string
}

export interface SessionListResponse {
  items: SessionItem[]
}

export interface HistoryMessage {
  role: string
  content: string
}

export interface HistoryResponse {
  thread_id: string
  messages: HistoryMessage[]
}

// SSE 对话事件载荷（与 backend/app/api/routes.py 对齐）
export type SseEvent =
  | { event: 'start'; data: { thread_id: string; user_id: string; tenant_id: string } }
  | { event: 'token'; data: { content: string } }
  | { event: 'tool_start'; data: { tool: string; args: unknown } }
  | { event: 'tool_end'; data: { tool: string; result: string } }
  | { event: 'subagent_start'; data: { agent: string; description: string } }
  | { event: 'subagent_end'; data: { agent: string; report: string } }
  | { event: 'memory'; data: { text: string } }
  | {
      event: 'citation'
      data: {
        contexts: RagContext[]
        citation: RagCitation[]
        mode: string
        elapsed_ms: number
        error?: string | null
      }
    }
  | { event: 'done'; data: { thread_id: string; content: string } }
  | { event: 'error'; data: { message: string } }

/** RAG 检索上下文片段（与 backend RAGContext 对齐） */
export interface RagContext {
  content: string
  score: number
  source: string
  metadata: Record<string, unknown>
}

/** RAG 引用元信息（与 backend RAGCitation 对齐） */
export interface RagCitation {
  doc_id: string
  title: string
  page: number | null
  chunk_id: string
  url: string
  metadata: Record<string, unknown>
}

export interface ChatMessage {
  role: 'user' | 'ai' | 'event' | 'citation'
  content: string
  subtype?: '' | 'subagent' | 'memory'
  /** citation 类型消息携带的引用列表 */
  citations?: RagCitation[]
  /** citation 类型消息携带的上下文片段 */
  contexts?: RagContext[]
  /** citation 类型消息的检索模式（stub/live/degraded） */
  ragMode?: string
  /** citation 类型消息的错误信息 */
  ragError?: string | null
}

// ===== V2：Workflow / 编译 / 编排 / 检查点 =====

/** 画布节点类型（与后端 compiler.py NODE_TYPES 对齐） */
export type WorkflowNodeType = 'start' | 'agent' | 'tool' | 'subagent' | 'end'

/** 画布节点 data：按 type 解读不同字段 */
export interface WorkflowNodeData {
  system_prompt?: string
  model?: string
  tools?: string[]
  skills?: string[]
  tool?: string
  task_type?: string
  subagents?: WorkflowSubagentSpec[]
}

/** subagent 节点 data.subagents 的单条 spec */
export interface WorkflowSubagentSpec {
  name: string
  description?: string
  system_prompt?: string
  model?: string
  tools?: string[]
}

/** 画布节点（Vue Flow 对齐） */
export interface WorkflowNode {
  id: string
  type: WorkflowNodeType
  data: WorkflowNodeData
  position: { x: number; y: number }
}

/** 画布连线 */
export interface WorkflowEdge {
  id: string
  source: string
  target: string
}

/** 画布定义 */
export interface WorkflowDefinition {
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
}

/** Workflow 元数据（列表 / 单查） */
export interface WorkflowItem {
  id: string
  tenant_id: string
  name: string
  description: string
  active_version: number
  is_deployed: boolean
  created_at: string
  updated_at: string
}

export interface WorkflowListResponse {
  items: WorkflowItem[]
}

export interface WorkflowCreateRequest {
  name: string
  description?: string
  definition: WorkflowDefinition
}

export interface WorkflowUpdateRequest {
  name?: string
  description?: string
  definition?: WorkflowDefinition
  is_deployed?: boolean
}

export interface WorkflowVersionItem {
  id: string
  workflow_id: string
  version: number
  definition: string // JSON 字符串
  compiled_config: string | null // JSON 字符串
  created_at: string
}

export interface WorkflowVersionsResponse {
  items: WorkflowVersionItem[]
}

/** 编译产物 */
export interface CompiledConfig {
  system_prompt: string
  model: string
  tools: string[]
  skills: string[]
  subagents: WorkflowSubagentSpec[]
  topological_order: string[]
}

export interface CompiledConfigResponse {
  workflow_id: string
  version: number
  config: CompiledConfig
}

export interface CompileRequest {
  definition: WorkflowDefinition
}

/** sequential 编排结果 */
export interface OrchestratorStepItem {
  agent: string
  step: number
  input: string
  output: string
  ok: boolean
  error: string | null
}

export interface OrchestratorRunRequest {
  task: string
  context?: Record<string, unknown>
}

export interface OrchestratorRunResponse {
  steps: OrchestratorStepItem[]
  final_output: string
  partial: boolean
  error: string | null
}

/** 检查点 */
export interface CheckpointCreateRequest {
  label?: string
  workflow_id?: string | null
}

export interface CheckpointItem {
  id: string
  tenant_id: string
  workflow_id: string | null
  source_thread_id: string
  target_thread_id: string | null
  label: string
  created_at: string
}

export interface CheckpointListResponse {
  items: CheckpointItem[]
}

export interface CheckpointRestoreResponse {
  checkpoint_id: string
  source_thread_id: string
  target_thread_id: string
}
