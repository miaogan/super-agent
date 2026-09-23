<script setup lang="ts">
// 子代理管理页签：自定义子代理 + 复用到 workflow
// - 列表：内置镜像（is_builtin）+ 自定义
// - 创建/编辑/删除/Fork 内置
// - 一键「加入当前 workflow subagent 节点」（仅当选中 subagent 节点时可用）

import { computed, onMounted, ref } from 'vue'
import { useSubAgentsStore } from '@/stores/subagents'
import { useWorkflowStore } from '@/stores/workflow'
import type { SubAgentItem } from '@/types'

const store = useSubAgentsStore()
const wf = useWorkflowStore()

// 当前编辑表单
const editing = ref(false)
const editingId = ref<string | null>(null)
const form = ref({
  name: '',
  description: '',
  system_prompt: '',
  model: '',
  tools: '', // 逗号分隔输入
})

// Fork 弹窗
const forkTarget = ref<SubAgentItem | null>(null)
const forkName = ref('')

// 内置 / 自定义分组
const builtinItems = computed(() => store.items.filter((x) => x.is_builtin))
const customItems = computed(() => store.items.filter((x) => !x.is_builtin))

// 当前选中的 workflow subagent 节点（用于「加入节点」）
const selectedSubNode = computed(() => {
  const n = wf.selectedNode
  return n && n.type === 'subagent' ? n : null
})

function resetForm() {
  form.value = {
    name: '',
    description: '',
    system_prompt: '',
    model: '',
    tools: '',
  }
  editingId.value = null
  editing.value = false
}

function startCreate() {
  resetForm()
  editing.value = true
}

function startEdit(item: SubAgentItem) {
  editingId.value = item.id
  form.value = {
    name: item.name,
    description: item.description,
    system_prompt: item.system_prompt,
    model: item.model,
    tools: item.tools.join(', '),
  }
  editing.value = true
}

function startFork(item: SubAgentItem) {
  forkTarget.value = item
  forkName.value = `${item.name}_copy`
}

async function onSave() {
  if (!form.value.name.trim()) {
    alert('名称必填')
    return
  }
  const payload = {
    name: form.value.name.trim(),
    description: form.value.description,
    system_prompt: form.value.system_prompt,
    model: form.value.model.trim(),
    tools: form.value.tools
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
  }
  try {
    if (editingId.value) {
      await store.update(editingId.value, payload)
    } else {
      await store.create(payload)
    }
    resetForm()
  } catch (e) {
    alert((e as Error).message)
  }
}

async function onFork() {
  if (!forkTarget.value) return
  if (!forkName.value.trim()) {
    alert('新名称必填')
    return
  }
  try {
    await store.fork(forkTarget.value.id, forkName.value.trim())
    forkTarget.value = null
    forkName.value = ''
  } catch (e) {
    alert((e as Error).message)
  }
}

async function onDelete(item: SubAgentItem) {
  if (!confirm(`确认删除子代理「${item.name}」？`)) return
  try {
    await store.remove(item.id)
  } catch (e) {
    alert((e as Error).message)
  }
}

/** 把子代理加入当前选中的 subagent 节点 */
function addIntoWorkflowNode(item: SubAgentItem) {
  if (!selectedSubNode.value) return
  const next = [
    ...(selectedSubNode.value.data.subagents || []),
    {
      name: item.name,
      description: item.description,
      system_prompt: item.system_prompt,
      model: item.model || undefined,
      tools: item.tools.length ? item.tools : undefined,
    },
  ]
  wf.updateNodeData(selectedSubNode.value.id, { subagents: next })
}

onMounted(() => {
  store.load()
})
</script>

<template>
  <div class="manager">
    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <div class="left">
        <h2>子代理管理</h2>
        <span class="count">共 {{ store.items.length }} 个</span>
      </div>
      <div class="right">
        <button class="btn ghost" @click="store.load()">刷新</button>
        <button class="btn primary" @click="startCreate">+ 新建子代理</button>
      </div>
    </div>

    <div v-if="store.loading" class="empty">加载中…</div>
    <div v-else-if="store.error" class="err">{{ store.error }}</div>

    <!-- 编辑表单 -->
    <div v-if="editing" class="form-card">
      <h3>{{ editingId ? '编辑子代理' : '新建子代理' }}</h3>
      <div class="field">
        <label>名称</label>
        <input v-model="form.name" placeholder="如：code-reviewer" class="inp" />
      </div>
      <div class="field">
        <label>一句话描述</label>
        <input
          v-model="form.description"
          placeholder="用于 LLM 判断何时派发该子代理"
          class="inp"
        />
      </div>
      <div class="field">
        <label>System Prompt</label>
        <textarea
          v-model="form.system_prompt"
          placeholder="子代理的角色定义、工作准则、输出格式要求"
          class="ta"
        />
      </div>
      <div class="field">
        <label>Model（留空 = 继承主 agent）</label>
        <input v-model="form.model" placeholder="auto / openai:gpt-4o-mini" class="inp" />
      </div>
      <div class="field">
        <label>Tools（逗号分隔，留空 = 继承主 agent 工具集）</label>
        <input v-model="form.tools" placeholder="sandbox,memory,search" class="inp" />
      </div>
      <div class="actions">
        <button class="btn primary" @click="onSave">保存</button>
        <button class="btn ghost" @click="resetForm">取消</button>
      </div>
    </div>

    <!-- Fork 弹窗 -->
    <div v-if="forkTarget" class="form-card">
      <h3>Fork 子代理「{{ forkTarget.name }}」</h3>
      <div class="field">
        <label>新名称</label>
        <input v-model="forkName" placeholder="新子代理名称" class="inp" />
      </div>
      <div class="hint">基于「{{ forkTarget.name }}」复制一份可编辑副本（自定义标记）。</div>
      <div class="actions">
        <button class="btn primary" @click="onFork">Fork</button>
        <button class="btn ghost" @click="forkTarget = null">取消</button>
      </div>
    </div>

    <!-- 内置子代理 -->
    <div class="section">
      <h3>内置子代理（镜像）</h3>
      <div class="hint">来自代码内置 SUBAGENTS，可 Fork 后定制；可直接复用到 workflow。</div>
      <div v-if="builtinItems.length === 0" class="empty">暂无</div>
      <div v-for="item in builtinItems" :key="item.id" class="row">
        <div class="row-main">
          <div class="row-head">
            <span class="badge builtin">内置</span>
            <span class="name">{{ item.name }}</span>
            <span class="model" v-if="item.model">· {{ item.model }}</span>
          </div>
          <div class="desc">{{ item.description }}</div>
          <pre v-if="item.system_prompt" class="prompt">{{
            item.system_prompt
          }}</pre>
          <div class="tools" v-if="item.tools.length">
            tools:
            <span v-for="t in item.tools" :key="t" class="tool-tag">{{ t }}</span>
          </div>
        </div>
        <div class="row-actions">
          <button class="btn small" @click="addIntoWorkflowNode(item)">
            加入节点
          </button>
          <button class="btn ghost small" @click="startFork(item)">Fork</button>
          <button class="btn ghost small" @click="startEdit(item)">编辑镜像</button>
        </div>
      </div>
    </div>

    <!-- 自定义子代理 -->
    <div class="section">
      <h3>自定义子代理</h3>
      <div v-if="customItems.length === 0" class="empty">
        暂无自定义子代理，点右上角「新建」或 Fork 内置
      </div>
      <div v-for="item in customItems" :key="item.id" class="row">
        <div class="row-main">
          <div class="row-head">
            <span class="badge custom">自定义</span>
            <span class="name">{{ item.name }}</span>
            <span class="model" v-if="item.model">· {{ item.model }}</span>
          </div>
          <div class="desc">{{ item.description }}</div>
          <pre v-if="item.system_prompt" class="prompt">{{
            item.system_prompt
          }}</pre>
          <div class="tools" v-if="item.tools.length">
            tools:
            <span v-for="t in item.tools" :key="t" class="tool-tag">{{ t }}</span>
          </div>
        </div>
        <div class="row-actions">
          <button class="btn small" @click="addIntoWorkflowNode(item)">
            加入节点
          </button>
          <button class="btn ghost small" @click="startEdit(item)">编辑</button>
          <button class="btn danger small" @click="onDelete(item)">删除</button>
        </div>
      </div>
    </div>

    <!-- 复用到 workflow 提示 -->
    <div v-if="selectedSubNode" class="hint-card">
      检测到已选中 workflow 中的 subagent 节点「{{
        selectedSubNode.id
      }}」，点击行内「加入节点」即可一键复用。
    </div>
    <div v-else class="hint-card muted">
      提示：在 Workflow 画布选中一个 subagent 节点，再回到此页签点击「加入节点」即可复用。
    </div>
  </div>
</template>

<style scoped>
.manager {
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
.btn.danger {
  color: #ff6b6b;
  border-color: #ff6b6b;
}
.btn.danger:hover {
  background: #ff6b6b;
  color: #fff;
}
.form-card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 24px;
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
.ta {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 8px;
  font-size: 13px;
  box-sizing: border-box;
}
.ta {
  min-height: 100px;
  resize: vertical;
  font-family: monospace;
}
.actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.hint {
  font-size: 11px;
  color: var(--muted);
  margin-top: 4px;
}
.section {
  margin-bottom: 24px;
}
.section h3 {
  font-size: 13px;
  color: var(--muted);
  margin: 0 0 8px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.empty {
  color: var(--muted);
  font-size: 12px;
  padding: 12px;
}
.err {
  color: #ff6b6b;
  font-size: 12px;
}
.row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
  margin-bottom: 8px;
}
.row-main {
  flex: 1;
  min-width: 0;
}
.row-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 4px;
}
.row-head .name {
  font-weight: 600;
  font-size: 14px;
}
.row-head .model {
  font-size: 11px;
  color: var(--muted);
}
.badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  color: #fff;
}
.badge.builtin {
  background: var(--muted);
}
.badge.custom {
  background: var(--accent);
}
.desc {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 6px;
}
.prompt {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 160px;
  overflow-y: auto;
  margin: 0 0 6px;
}
.tools {
  font-size: 11px;
  color: var(--muted);
}
.tool-tag {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 6px;
  margin: 0 4px;
  font-size: 10px;
}
.row-actions {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex-shrink: 0;
}
.hint-card {
  margin-top: 24px;
  padding: 12px;
  background: var(--panel);
  border: 1px solid var(--accent);
  border-radius: 8px;
  font-size: 12px;
  color: var(--accent);
}
.hint-card.muted {
  border-color: var(--border);
  color: var(--muted);
}
</style>
