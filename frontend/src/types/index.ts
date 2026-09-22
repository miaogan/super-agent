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
  | { event: 'done'; data: { thread_id: string; content: string } }
  | { event: 'error'; data: { message: string } }

export interface ChatMessage {
  role: 'user' | 'ai' | 'event'
  content: string
  subtype?: '' | 'subagent' | 'memory'
}
