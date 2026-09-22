// 后端 API 封装：fetch + JWT；SSE 用 ReadableStream 解析

import type {
  AgentsResponse,
  HistoryResponse,
  LoginRequest,
  LoginResponse,
  MemoriesResponse,
  MemoryItem,
  RegisterRequest,
  RegisterResponse,
  SessionListResponse,
  SkillCreateRequest,
  SkillItem,
  SkillListResponse,
  SseEvent,
} from '@/types'

const TOKEN_KEY = 'access_token'
const TENANT_KEY = 'tenant_id'
const USER_KEY = 'user_id'
const EMAIL_KEY = 'email'

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
