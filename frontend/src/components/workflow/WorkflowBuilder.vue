<script setup lang="ts">
// V2 Workflow 构建器：工具栏 + 画布 + 编译预览 + 编排测试面板

import { ref } from 'vue'
import WorkflowCanvas from './WorkflowCanvas.vue'
import { useWorkflowStore } from '@/stores/workflow'

const wf = useWorkflowStore()

// 工具栏状态
const newName = ref('')
const newDesc = ref('')
const showSaveDialog = ref(false)

// 编排测试
const orchTask = ref('')
const orchResult = ref<string>('')

async function onCompile() {
  await wf.previewCompile()
}

async function onSaveAs() {
  if (!newName.value.trim()) {
    alert('请填写 workflow 名称')
    return
  }
  const wf_item = await wf.saveAs(newName.value, newDesc.value)
  if (wf_item) {
    showSaveDialog.value = false
    newName.value = ''
    newDesc.value = ''
  }
}

async function onSaveUpdate() {
  if (!wf.currentWorkflowId) {
    showSaveDialog.value = true
    return
  }
  await wf.saveUpdate()
}

async function onDeploy() {
  if (!wf.currentWorkflowId) {
    alert('请先保存 workflow')
    return
  }
  const wf_item = await wf.deploy()
  if (wf_item) {
    alert('部署成功，可在对话中使用')
  }
}

async function onOrchestrate() {
  if (!orchTask.value.trim()) {
    alert('请填写任务描述')
    return
  }
  const res = await wf.runOrchestrate(orchTask.value)
  if (res) {
    orchResult.value = JSON.stringify(res, null, 2)
  }
}

function onClear() {
  if (confirm('清空画布？未保存的更改将丢失')) {
    wf.clearCanvas()
    orchResult.value = ''
  }
}

function onNewWorkflow() {
  wf.clearCanvas()
  orchResult.value = ''
}

async function onLoadWorkflow(id: string) {
  await wf.loadFromServer(id)
  await wf.loadCompiledConfig()
}

import { onMounted } from 'vue'
onMounted(() => {
  wf.loadWorkflows()
})
</script>

<template>
  <div class="builder">
    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <div class="left">
        <button class="btn" @click="onNewWorkflow">新建</button>
        <button class="btn" @click="showSaveDialog = true">另存为</button>
        <button
          class="btn"
          :disabled="!wf.currentWorkflowId"
          @click="onSaveUpdate"
        >
          保存
        </button>
        <button
          class="btn primary"
          :disabled="!wf.currentWorkflowId"
          @click="onDeploy"
        >
          部署
        </button>
        <span class="sep" />
        <button class="btn" :disabled="wf.compiling" @click="onCompile">
          {{ wf.compiling ? '编译中…' : '编译预览' }}
        </button>
        <button class="btn ghost" @click="onClear">清空</button>
      </div>
      <div class="right">
        <span v-if="wf.currentWorkflow" class="wf-meta">
          {{ wf.currentWorkflow.name }}
          <span class="badge" :class="{ on: wf.currentWorkflow.is_deployed }">
            {{ wf.currentWorkflow.is_deployed ? '已部署' : '草稿' }}
          </span>
          v{{ wf.currentWorkflow.active_version }}
        </span>
      </div>
    </div>

    <!-- 已保存 workflow 列表 -->
    <div class="wf-list">
      <details>
        <summary>已保存的 Workflow（{{ wf.workflows.length }}）</summary>
        <div class="wf-items">
          <div
            v-for="w in wf.workflows"
            :key="w.id"
            class="wf-item"
            :class="{ active: w.id === wf.currentWorkflowId }"
            @click="onLoadWorkflow(w.id)"
          >
            <span class="wf-name">{{ w.name }}</span>
            <span class="badge" :class="{ on: w.is_deployed }">
              {{ w.is_deployed ? '部署' : '草稿' }}
            </span>
            <span class="wf-ver">v{{ w.active_version }}</span>
            <button
              class="del"
              @click.stop="wf.remove(w.id)"
            >
              ✕
            </button>
          </div>
          <div v-if="wf.workflows.length === 0" class="empty">暂无</div>
        </div>
      </details>
    </div>

    <!-- 画布主体 -->
    <div class="canvas-wrap">
      <WorkflowCanvas />
    </div>

    <!-- 底部：编译产物 + 编排测试 -->
    <div class="bottom-panel">
      <div class="compile-view">
        <h4>编译产物</h4>
        <div v-if="wf.compileError" class="err">{{ wf.compileError }}</div>
        <pre v-else-if="wf.compiledConfig" class="code">{{
          JSON.stringify(wf.compiledConfig, null, 2)
        }}</pre>
        <div v-else class="empty">点击「编译预览」查看 deepagents 配置</div>
      </div>

      <div class="orch-panel">
        <h4>Sequential 编排测试</h4>
        <div class="row">
          <input
            v-model="orchTask"
            placeholder="任务描述（如：写一个 demo）"
            class="inp"
          />
          <button
            class="btn primary"
            :disabled="wf.orchestrating || !wf.currentWorkflowId"
            @click="onOrchestrate"
          >
            {{ wf.orchestrating ? '执行中…' : '执行' }}
          </button>
        </div>
        <pre v-if="orchResult" class="code">{{ orchResult }}</pre>
        <div v-else class="empty">需要 workflow 含 subagent 节点</div>
      </div>
    </div>

    <!-- 另存为对话框 -->
    <div v-if="showSaveDialog" class="modal-bg" @click.self="showSaveDialog = false">
      <div class="modal">
        <h3>另存为 Workflow</h3>
        <label class="lbl">名称</label>
        <input v-model="newName" class="inp" placeholder="workflow 名称" />
        <label class="lbl">描述</label>
        <textarea v-model="newDesc" class="ta" placeholder="一句话描述" />
        <div class="modal-actions">
          <button class="btn ghost" @click="showSaveDialog = false">取消</button>
          <button class="btn primary" @click="onSaveAs">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.builder {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 16px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  gap: 12px;
}
.toolbar .left {
  display: flex;
  gap: 6px;
  align-items: center;
}
.btn {
  padding: 6px 14px;
  font-size: 12px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  border-radius: 6px;
  cursor: pointer;
}
.btn:hover:not(:disabled) {
  border-color: var(--accent);
}
.btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.btn.primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.btn.ghost {
  background: transparent;
}
.sep {
  width: 1px;
  height: 20px;
  background: var(--border);
  margin: 0 4px;
}
.wf-meta {
  font-size: 12px;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 6px;
}
.badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--border);
  color: var(--muted);
}
.badge.on {
  background: #10b981;
  color: #fff;
}
.wf-list {
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  padding: 4px 16px;
}
.wf-list summary {
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
  padding: 4px 0;
}
.wf-items {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 0;
}
.wf-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
  background: var(--panel);
}
.wf-item:hover {
  border-color: var(--accent);
}
.wf-item.active {
  border-color: var(--accent);
  background: var(--bg);
}
.wf-name {
  font-weight: 500;
}
.wf-ver {
  color: var(--muted);
  font-size: 10px;
}
.del {
  background: transparent;
  border: none;
  color: var(--muted);
  cursor: pointer;
  font-size: 10px;
}
.del:hover {
  color: #ff6b6b;
}
.empty {
  color: var(--muted);
  font-size: 12px;
}
.canvas-wrap {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
.bottom-panel {
  display: flex;
  border-top: 1px solid var(--border);
  height: 200px;
  flex-shrink: 0;
}
.compile-view,
.orch-panel {
  flex: 1;
  padding: 10px 14px;
  overflow-y: auto;
  border-right: 1px solid var(--border);
}
.orch-panel {
  border-right: none;
}
.bottom-panel h4 {
  font-size: 12px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}
.code {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  font-size: 11px;
  font-family: monospace;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 140px;
  overflow-y: auto;
  margin: 0;
}
.err {
  color: #ff6b6b;
  font-size: 12px;
}
.row {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}
.inp {
  flex: 1;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
}
.modal-bg {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.modal {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
  width: 380px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}
.modal h3 {
  font-size: 16px;
  margin-bottom: 12px;
}
.modal .lbl {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin: 8px 0 4px;
}
.modal .inp {
  width: 100%;
  box-sizing: border-box;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 8px;
  font-size: 13px;
}
.modal .ta {
  width: 100%;
  box-sizing: border-box;
  min-height: 50px;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 8px;
  font-size: 13px;
  resize: vertical;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
</style>
