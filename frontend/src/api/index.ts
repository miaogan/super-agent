// 后端 API 封装：fetch + JWT；SSE 用 ReadableStream 解析

import type {
  AgentsResponse,
  CheckpointCreateRequest,
  CheckpointItem,
  CheckpointListResponse,
  CheckpointRestoreResponse,
  CompileRequest,
  CompiledConfigResponse,
  HistoryResponse,
  LoginRequest,
  LoginResponse,
  MarketInstallResponse,
  MarketPublishRequest,
  MarketRateResponse,
  MarketTemplateItem,
  MarketTemplateListResponse,
  MemoriesResponse,
  MemoryItem,
  OrchestratorRunRequest,
  OrchestratorRunResponse,
  RegisterRequest,
  RegisterResponse,
  SessionListResponse,
  SkillCreateRequest,
  SkillItem,
  SkillListResponse,
  SseEvent,
  SubAgentCreateRequest,
  SubAgentItem,
  SubAgentListResponse,
  SubAgentUpdateRequest,
  WorkflowCreateRequest,
  WorkflowDefinition,
  WorkflowItem,
  WorkflowListResponse,
  WorkflowUpdateRequest,
  WorkflowVersionsResponse,
} from '@/types'

const TOKEN_KEY = 'access_token'
const TENANT_KEY = 'tenant_id'
const USER_KEY = 'user_id'
const EMAIL_KEY = 'email'

// API base URL：构建时通过 VITE_API_URL 注入
// - 同源部署（nginx 反代 /api）：留空，走相对路径 /api
// - 跨源部署（前后端不同主机）：填后端地址，如 http://10.0.0.10:8000
const API_BASE = import.meta.env.VITE_API_URL || ''

/**
 * 封装 fetch：自动拼接 API_BASE 前缀。
 * 所有调用处仍用 fetch('/api/...') 风格，由本函数注入 baseURL。
 */
const _fetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
  if (typeof input === 'string' && input.startsWith('/api')) {
    const url = API_BASE ? `${API_BASE}${input}` : input
    return _fetch(url, init)
  }
  return _fetch(input, init)
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function saveAuth(d: LoginResponse) {
  localStorage.setItem(TOKEN_KEY, d.access_token)
  localStorage.setItem(TENANT_KEY, d.tenant_id)
  localStorage.setItem(USER_KEY, d.user_id)
  localStorage.setItem(EMAIL_KEY, d.email)
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(TENANT_KEY)
  localStorage.removeItem(USER_KEY)
  localStorage.removeItem(EMAIL_KEY)
}

export function getStoredEmail(): string | null {
  return localStorage.getItem(EMAIL_KEY)
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = { ...extra }
  const t = getToken()
  if (t) h['Authorization'] = 'Bearer ' + t
  return h
}

async function handle<T>(r: Response): Promise<T> {
  if (r.status === 401) {
    clearAuth()
    throw new Error('未授权，请重新登录')
  }
  const data = await r.json()
  if (!r.ok) {
    const detail = (data as { detail?: string }).detail || JSON.stringify(data)
    throw new Error(detail)
  }
  return data as T
}

// ----- 认证 -----

export async function registerTenant(req: RegisterRequest): Promise<RegisterResponse> {
  const r = await fetch('/api/v1/tenants/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  return handle<RegisterResponse>(r)
}

export async function login(req: LoginRequest): Promise<LoginResponse> {
  const r = await fetch('/api/v1/tenants/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  return handle<LoginResponse>(r)
}

// ----- Agent / Skill -----

export async function listAgents(): Promise<AgentsResponse> {
  const r = await fetch('/api/agents')
  return handle<AgentsResponse>(r)
}

export async function listSkills(): Promise<SkillListResponse> {
  const r = await fetch('/api/v1/skills', { headers: authHeaders() })
  return handle<SkillListResponse>(r)
}

export async function createSkill(req: SkillCreateRequest): Promise<SkillItem> {
  const r = await fetch('/api/v1/skills', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(req),
  })
  return handle<SkillItem>(r)
}

export async function deleteSkill(name: string): Promise<void> {
  const r = await fetch('/api/v1/skills/' + encodeURIComponent(name), {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!r.ok) throw new Error('删除失败')
}

/** 上传 zip 压缩包注册复杂 skill（多文件） */
export async function uploadSkillArchive(
  name: string,
  description: string,
  file: File,
): Promise<unknown> {
  const form = new FormData()
  form.append('name', name)
  form.append('description', description)
  form.append('file', file)
  const r = await fetch('/api/v1/skills/upload-archive', {
    method: 'POST',
    headers: authHeaders(), // 不设 Content-Type，让浏览器自动加 multipart boundary
    body: form,
  })
  return handle<unknown>(r)
}

// ----- V2.5：子代理管理 -----

/** 列出当前租户的全部子代理（含内置镜像 + 自定义） */
export async function listSubAgents(): Promise<SubAgentListResponse> {
  const r = await fetch('/api/v2/subagents', { headers: authHeaders() })
  return handle<SubAgentListResponse>(r)
}

export async function createSubAgent(
  req: SubAgentCreateRequest,
): Promise<SubAgentItem> {
  const r = await fetch('/api/v2/subagents', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(req),
  })
  return handle<SubAgentItem>(r)
}

export async function updateSubAgent(
  id: string,
  req: SubAgentUpdateRequest,
): Promise<SubAgentItem> {
  const r = await fetch('/api/v2/subagents/' + encodeURIComponent(id), {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(req),
  })
  return handle<SubAgentItem>(r)
}

export async function deleteSubAgent(id: string): Promise<void> {
  const r = await fetch('/api/v2/subagents/' + encodeURIComponent(id), {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!r.ok) throw new Error('删除失败')
}

/** 基于现有子代理（含内置）fork 一份自定义副本 */
export async function forkSubAgent(id: string, newName: string): Promise<SubAgentItem> {
  const r = await fetch(
    `/api/v2/subagents/${encodeURIComponent(id)}/fork?new_name=${encodeURIComponent(newName)}`,
    { method: 'POST', headers: authHeaders() },
  )
  return handle<SubAgentItem>(r)
}

// ----- 记忆 -----

export async function listMemories(): Promise<MemoriesResponse> {
  const r = await fetch('/api/memories', { headers: authHeaders() })
  return handle<MemoriesResponse>(r)
}

export async function addMemory(text: string): Promise<MemoryItem> {
  const r = await fetch('/api/memories', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ text }),
  })
  return handle<MemoryItem>(r)
}

export async function deleteMemory(key: string): Promise<void> {
  const r = await fetch('/api/memories/' + encodeURIComponent(key), {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!r.ok) throw new Error('删除失败')
}

// ----- 会话 -----

export async function listSessions(): Promise<SessionListResponse> {
  const r = await fetch('/api/v1/sessions', { headers: authHeaders() })
  return handle<SessionListResponse>(r)
}

export async function getSessionHistory(threadId: string): Promise<HistoryResponse> {
  const r = await fetch('/api/threads/' + threadId + '/history', {
    headers: authHeaders(),
  })
  return handle<HistoryResponse>(r)
}

export async function destroySandbox(threadId: string): Promise<void> {
  await fetch('/api/threads/' + threadId + '/sandbox', {
    method: 'DELETE',
    headers: authHeaders(),
  })
}

// ----- SSE 对话 -----

/**
 * 发起对话并流式解析 SSE。
 * 调用方提供 onEvent 回调，逐事件消费；返回时表示流结束。
 */
export async function streamChat(
  message: string,
  threadId: string | null,
  onEvent: (e: SseEvent) => void,
): Promise<void> {
  const r = await fetch('/api/chat', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ message, thread_id: threadId }),
  })
  if (r.status === 401) {
    clearAuth()
    throw new Error('未授权，请重新登录')
  }
  if (!r.body) throw new Error('无响应流')

  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let sseBuf = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    sseBuf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = sseBuf.indexOf('\n\n')) >= 0) {
      const frame = sseBuf.slice(0, idx)
      sseBuf = sseBuf.slice(idx + 2)
      let event = 'message'
      let data = ''
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (!data) continue
      let payload: unknown
      try {
        payload = JSON.parse(data)
      } catch {
        continue
      }
      onEvent({ event, data: payload } as SseEvent)
    }
  }
}

// ----- V2：Workflow CRUD + 编译 + 编排 + 检查点 -----

export async function listWorkflows(): Promise<WorkflowListResponse> {
  const r = await fetch('/api/v2/workflows', { headers: authHeaders() })
  return handle<WorkflowListResponse>(r)
}

export async function getWorkflow(id: string): Promise<WorkflowItem> {
  const r = await fetch('/api/v2/workflows/' + encodeURIComponent(id), {
    headers: authHeaders(),
  })
  return handle<WorkflowItem>(r)
}

export async function createWorkflow(req: WorkflowCreateRequest): Promise<WorkflowItem> {
  const r = await fetch('/api/v2/workflows', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(req),
  })
  return handle<WorkflowItem>(r)
}

export async function updateWorkflow(
  id: string,
  req: WorkflowUpdateRequest,
): Promise<WorkflowItem> {
  const r = await fetch('/api/v2/workflows/' + encodeURIComponent(id), {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(req),
  })
  return handle<WorkflowItem>(r)
}

export async function deleteWorkflow(id: string): Promise<void> {
  const r = await fetch('/api/v2/workflows/' + encodeURIComponent(id), {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!r.ok) throw new Error('删除失败')
}

export async function listWorkflowVersions(id: string): Promise<WorkflowVersionsResponse> {
  const r = await fetch('/api/v2/workflows/' + encodeURIComponent(id) + '/versions', {
    headers: authHeaders(),
  })
  return handle<WorkflowVersionsResponse>(r)
}

export async function activateWorkflowVersion(
  id: string,
  version: number,
): Promise<WorkflowItem> {
  const r = await fetch(
    `/api/v2/workflows/${encodeURIComponent(id)}/activate/${version}`,
    { method: 'POST', headers: authHeaders() },
  )
  return handle<WorkflowItem>(r)
}

/** 实时编译预览（不落库）：画布编辑器边拖边校验 */
export async function compilePreview(
  req: CompileRequest,
): Promise<CompiledConfigResponse> {
  const r = await fetch('/api/v2/workflows/compile', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(req),
  })
  return handle<CompiledConfigResponse>(r)
}

/** 读取已落库 workflow 的 active 版本编译产物 */
export async function getCompiledConfig(
  id: string,
): Promise<CompiledConfigResponse> {
  const r = await fetch('/api/v2/workflows/' + encodeURIComponent(id) + '/config', {
    headers: authHeaders(),
  })
  return handle<CompiledConfigResponse>(r)
}

/** sequential 编排：按 active 版本编译产物串行执行 subagents */
export async function orchestrateWorkflow(
  id: string,
  req: OrchestratorRunRequest,
): Promise<OrchestratorRunResponse> {
  const r = await fetch(
    '/api/v2/workflows/' + encodeURIComponent(id) + '/orchestrate',
    {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(req),
    },
  )
  return handle<OrchestratorRunResponse>(r)
}

/** parallel 编排：fan-out 并行执行 subagents
 *
 * V3-T7 扩展：strategy 支持 first/all/merge/vote/judge；
 * judge 策略可传 judge_spec 自定义法官。
 */
export async function runParallelWorkflow(
  id: string,
  req: OrchestratorRunRequest & {
    strategy?: string
    judge_spec?: Record<string, unknown>
  },
): Promise<OrchestratorRunResponse & {
  vote_counts?: Record<string, number> | null
  winner_vote?: string | null
  judge_agent?: string | null
}> {
  const r = await fetch(
    '/api/v2/workflows/' + encodeURIComponent(id) + '/orchestrate-parallel',
    {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(req),
    },
  )
  return handle<OrchestratorRunResponse>(r)
}

// ----- V2：检查点 -----

export async function createCheckpoint(
  threadId: string,
  req: CheckpointCreateRequest,
): Promise<CheckpointItem> {
  const r = await fetch(
    '/api/v2/threads/' + encodeURIComponent(threadId) + '/checkpoints',
    {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(req),
    },
  )
  return handle<CheckpointItem>(r)
}

export async function listCheckpoints(
  threadId: string,
): Promise<CheckpointListResponse> {
  const r = await fetch(
    '/api/v2/threads/' + encodeURIComponent(threadId) + '/checkpoints',
    { headers: authHeaders() },
  )
  return handle<CheckpointListResponse>(r)
}

export async function restoreCheckpoint(
  threadId: string,
  checkpointId: string,
): Promise<CheckpointRestoreResponse> {
  const r = await fetch(
    `/api/v2/threads/${encodeURIComponent(threadId)}/checkpoints/${encodeURIComponent(checkpointId)}/restore`,
    { method: 'POST', headers: authHeaders() },
  )
  return handle<CheckpointRestoreResponse>(r)
}

// ----- V3-T6：模板市场 -----

/** 列出模板市场（全部租户发布，按安装量排序） */
export async function listMarketTemplates(
  type?: string,
): Promise<MarketTemplateListResponse> {
  const q = type ? `?type=${encodeURIComponent(type)}` : ''
  const r = await fetch('/api/v3/market' + q, { headers: authHeaders() })
  return handle<MarketTemplateListResponse>(r)
}

/** 把自有资源（workflow / skill / prompt）打包上架 */
export async function publishMarketTemplate(
  req: MarketPublishRequest,
): Promise<MarketTemplateItem> {
  const r = await fetch('/api/v3/market/publish', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(req),
  })
  return handle<MarketTemplateItem>(r)
}

/** 把市场模板安装到当前租户 */
export async function installMarketTemplate(
  templateId: string,
): Promise<MarketInstallResponse> {
  const r = await fetch(
    '/api/v3/market/' + encodeURIComponent(templateId) + '/install',
    { method: 'POST', headers: authHeaders() },
  )
  return handle<MarketInstallResponse>(r)
}

/** 给模板评分（1-5，重复评分覆盖） */
export async function rateMarketTemplate(
  templateId: string,
  score: number,
): Promise<MarketRateResponse> {
  const r = await fetch(
    '/api/v3/market/' + encodeURIComponent(templateId) + '/rate',
    {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ score }),
    },
  )
  return handle<MarketRateResponse>(r)
}
