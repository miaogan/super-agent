<script setup lang="ts">
// V3-T4 / V3-T5 A2A 协议页签：卡片注册 / 发现 / 任务派发

import { onMounted, ref } from 'vue'
import * as api from '@/api'
import type { A2AAgentItem, A2ATaskItem } from '@/types'

const agents = ref<A2AAgentItem[]>([])
const discovered = ref<A2AAgentItem[]>([])
const tasks = ref<A2ATaskItem[]>([])
const loading = ref(false)
const error = ref('')

// 注册表单
const showRegister = ref(false)
const regName = ref('')
const regDesc = ref('')
const regUrl = ref('')
const regCaps = ref('')
const registering = ref(false)

// 发现
const capabilityFilter = ref('')

// 派发
const dispatchAgentId = ref('')
const dispatchTask = ref('')
const dispatchAsync = ref(false)
const dispatching = ref(false)

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const [a, d, t] = await Promise.all([
      api.listA2AAgents(),
      api.discoverA2AAgents(capabilityFilter.value || undefined),
      api.listA2ATasks(),
    ])
    agents.value = a.items
    discovered.value = d.items
    tasks.value = t.items
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

function resetRegister() {
  regName.value = ''
  regDesc.value = ''
  regUrl.value = ''
  regCaps.value = ''
}

async function onRegister() {
  if (!regName.value.trim() || !regUrl.value.trim()) {
    alert('名称和 URL 必填')
    return
  }
  registering.value = true
  try {
    await api.createA2AAgent({
      name: regName.value.trim(),
      description: regDesc.value,
      url: regUrl.value.trim(),
      capabilities: regCaps.value
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
    })
    showRegister.value = false
    resetRegister()
    await loadAll()
    alert('注册成功')
  } catch (e) {
    alert((e as Error).message)
  } finally {
    registering.value = false
  }
}

async function onDelete(a: A2AAgentItem) {
  try {
    await api.deleteA2AAgent(a.id)
    await loadAll()
  } catch (e) {
    alert((e as Error).message)
  }
}

async function onDemoSetup() {
  try {
    await api.setupA2ADemo()
    await loadAll()
    alert('已注册 3 个演示代理（echo / reviewer / translator）')
  } catch (e) {
    alert((e as Error).message)
  }
}

async function onDispatch() {
  const agent = agents.value.find((a) => a.id === dispatchAgentId.value)
  if (!agent || !dispatchTask.value.trim()) {
    alert('请选择代理并填写任务')
    return
  }
  dispatching.value = true
  try {
    const r = await api.dispatchA2ATask(agent.id, {
      task: dispatchTask.value.trim(),
      async_dispatch: dispatchAsync.value,
    })
    dispatchTask.value = ''
    await loadAll()
    alert(`任务已派发：${r.task.id}（${r.task.status}）`)
  } catch (e) {
    alert((e as Error).message)
  } finally {
    dispatching.value = false
  }
}

async function onRefreshTask(t: A2ATaskItem) {
  try {
    const r = await api.getA2ATask(t.id)
    await loadAll()
    alert(
      `${r.task.status === 'completed' ? r.task.result : r.task.status}${r.task.error ? ' · ' + r.task.error : ''}`,
    )
  } catch (e) {
    alert((e as Error).message)
  }
}

async function onCancelTask(t: A2ATaskItem) {
  try {
    await api.cancelA2ATask(t.id)
    await loadAll()
  } catch (e) {
    alert((e as Error).message)
  }
}

const statusClass = (s: string) =>
  s === 'completed' ? 'ok' : s === 'failed' || s === 'cancelled' ? 'bad' : 'run'

onMounted(loadAll)
</script>

<template>
  <div class="a2a">
    <div class="toolbar">
      <div class="left">
        <h2>A2A 代理</h2>
        <span class="count">{{ agents.length }} 张卡片 · {{ tasks.length }} 个任务</span>
      </div>
      <div class="right">
        <button class="btn ghost" @click="loadAll">刷新</button>
        <button class="btn primary" @click="onDemoSetup">初始化演示代理</button>
        <button class="btn primary" @click="showRegister = !showRegister">
          {{ showRegister ? '收起注册' : '+ 注册卡片' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="error" class="err">{{ error }}</div>

    <!-- 注册表单 -->
    <div v-if="showRegister" class="card form-card">
      <h3>注册 A2A 外部 Agent 卡片</h3>
      <div class="field">
        <label>名称（唯一）</label>
        <input v-model="regName" class="inp" placeholder="如 code-reviewer" />
      </div>
      <div class="field">
        <label>JSON-RPC 端点 URL</label>
        <input v-model="regUrl" class="inp" placeholder="https://agent.example.com/jsonrpc" />
      </div>
      <div class="field">
        <label>能力声明（逗号分隔）</label>
        <input v-model="regCaps" class="inp" placeholder="code_review, translate" />
      </div>
      <div class="field">
        <label>描述</label>
        <input v-model="regDesc" class="inp" placeholder="一句话说明这个代理做什么" />
      </div>
      <div class="actions">
        <button class="btn primary" :disabled="registering" @click="onRegister">
          {{ registering ? '注册中…' : '注册' }}
        </button>
        <button class="btn ghost" @click="showRegister = false">取消</button>
      </div>
    </div>

    <!-- 我的卡片 -->
    <div class="section-title">我的卡片</div>
    <div v-if="agents.length === 0" class="empty small">
      暂无卡片 —— 可「初始化演示代理」或手动注册
    </div>
    <div v-for="a in agents" :key="a.id" class="card">
      <div class="card-head">
        <span class="badge" :class="a.status">{{ a.status }}</span>
        <span class="name">{{ a.name }}</span>
        <span class="ver">v{{ a.version }}</span>
        <button class="btn ghost small del" @click="onDelete(a)">注销</button>
      </div>
      <div class="desc">{{ a.description || '（无描述）' }}</div>
      <div class="meta">
        <span class="mono">{{ a.url }}</span>
      </div>
      <div class="caps">
        <span v-for="c in a.capabilities" :key="c" class="cap">{{ c }}</span>
        <span v-if="a.capabilities.length === 0" class="muted">无能力声明</span>
      </div>
    </div>

    <!-- 发现 -->
    <div class="section-title">
      可派发代理（跨租户发现）
      <input
        v-model="capabilityFilter"
        class="inp inline"
        placeholder="按能力过滤"
        @change="loadAll"
      />
    </div>
    <div v-if="discovered.length === 0" class="empty small">没有可发现的 active 代理</div>
    <div v-for="d in discovered" :key="d.id" class="card">
      <div class="card-head">
        <span class="badge active">active</span>
        <span class="name">{{ d.name }}</span>
      </div>
      <div class="desc">{{ d.description || '（无描述）' }}</div>
      <div class="caps">
        <span v-for="c in d.capabilities" :key="c" class="cap">{{ c }}</span>
      </div>
    </div>

    <!-- 派发 -->
    <div class="section-title">派发任务</div>
    <div class="card dispatch-card">
      <div class="row">
        <select v-model="dispatchAgentId" class="sel grow">
          <option value="" disabled>选择代理…</option>
          <option v-for="a in agents" :key="a.id" :value="a.id">{{ a.name }}</option>
        </select>
        <label class="chk">
          <input v-model="dispatchAsync" type="checkbox" /> 后台派发
        </label>
      </div>
      <div class="row">
        <input
          v-model="dispatchTask"
          class="inp grow"
          placeholder="任务内容，如：帮我评审这段代码"
          @keyup.enter="onDispatch"
        />
        <button class="btn primary" :disabled="dispatching" @click="onDispatch">
          {{ dispatching ? '派发中…' : '派发' }}
        </button>
      </div>
    </div>

    <!-- 任务列表 -->
    <div class="section-title">任务</div>
    <div v-if="tasks.length === 0" class="empty small">暂无任务</div>
    <div v-for="t in tasks" :key="t.id" class="card task">
      <div class="card-head">
        <span class="badge" :class="statusClass(t.status)">{{ t.status }}</span>
        <span class="name mono">{{ t.id }}</span>
        <span class="ver">→ {{ t.agent_name }}</span>
      </div>
      <div class="desc task-text">{{ t.task }}</div>
      <div v-if="t.result" class="result">{{ t.result }}</div>
      <div v-else-if="t.error" class="err">{{ t.error }}</div>
      <div class="meta">
        {{ new Date(t.created_at).toLocaleString() }}
        <button class="btn ghost small" @click="onRefreshTask(t)">刷新</button>
        <button
          v-if="t.status === 'submitted' || t.status === 'working'"
          class="btn ghost small"
          @click="onCancelTask(t)"
        >
          取消
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.a2a {
  padding: 16px 24px;
  overflow-y: auto;
  height: 100%;
  box-sizing: border-box;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.toolbar .left {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.toolbar h2 {
  font-size: 16px;
  margin: 0;
}
.toolbar .count {
  font-size: 12px;
  color: var(--muted);
}
.toolbar .right {
  display: flex;
  gap: 8px;
}
.btn {
  padding: 6px 12px;
  font-size: 12px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  cursor: pointer;
}
.btn.primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.btn.ghost {
  background: transparent;
}
.btn.small {
  padding: 4px 8px;
  font-size: 11px;
}
.section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 20px 0 8px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 16px;
  margin-bottom: 10px;
}
.form-card h3 {
  font-size: 14px;
  margin: 0 0 12px;
}
.field {
  margin-bottom: 12px;
}
.field label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 4px;
}
.inp,
.sel {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 8px;
  font-size: 13px;
  box-sizing: border-box;
}
.inp {
  width: 100%;
}
.inp.inline {
  width: 200px;
  margin-left: 8px;
  padding: 4px 8px;
  font-size: 12px;
}
.inp.grow,
.sel.grow {
  flex: 1;
}
.actions {
  display: flex;
  gap: 8px;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-head .name {
  font-weight: 600;
  font-size: 14px;
  flex: 1;
}
.ver {
  font-size: 11px;
  color: var(--muted);
}
.del {
  margin-left: auto;
}
.desc {
  font-size: 12px;
  color: var(--muted);
  margin-top: 6px;
}
.meta {
  font-size: 11px;
  color: var(--muted);
  margin-top: 4px;
}
.mono {
  font-family: monospace;
  font-size: 11px;
}
.caps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.cap {
  font-size: 10px;
  padding: 1px 8px;
  border-radius: 4px;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--accent);
}
.badge {
  font-size: 10px;
  padding: 1px 8px;
  border-radius: 4px;
  color: #fff;
}
.badge.active,
.badge.completed {
  background: #10b981;
}
.badge.working,
.badge.submitted {
  background: #f59e0b;
}
.badge.inactive,
.badge.failed,
.badge.cancelled {
  background: #ef4444;
}
.ok {
  background: #10b981;
}
.bad {
  background: #ef4444;
}
.run {
  background: #f59e0b;
}
.result {
  font-size: 12px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  margin-top: 8px;
  white-space: pre-wrap;
  word-break: break-all;
}
.task-text {
  color: var(--text);
}
.task .name {
  font-size: 12px;
}
.dispatch-card .row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
  align-items: center;
}
.chk {
  font-size: 12px;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.empty {
  color: var(--muted);
  font-size: 13px;
  text-align: center;
  padding: 40px 0;
}
.empty.small {
  padding: 12px 0;
  font-size: 12px;
}
.err {
  color: #ff6b6b;
  font-size: 12px;
}
.muted {
  color: var(--muted);
}
</style>
