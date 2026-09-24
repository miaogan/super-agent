<script setup lang="ts">
// Workflow 运行页签：专门用于快速验证 workflow 通路
// - 选择已保存的 workflow（含部署状态）
// - 输入测试任务描述
// - 选择执行模式（sequential / parallel）
// - 实时展示执行结果 + 错误信息
// - 历史运行记录

import { ref, computed, onMounted } from 'vue'
import { useWorkflowStore } from '@/stores/workflow'
import * as api from '@/api'

const wf = useWorkflowStore()

// 选中的 workflow id
const selectedId = ref('')
// 测试输入
const taskInput = ref('')
// 执行模式（V3-T7 新增 vote / judge）
const runMode = ref<'sequential' | 'parallel' | 'vote' | 'judge'>('sequential')
// 执行结果
const runResult = ref<unknown>(null)
const runError = ref('')
const running = ref(false)

// 历史运行记录
interface RunHistory {
  workflowId: string
  workflowName: string
  task: string
  mode: string
  success: boolean
  result: unknown
  error: string
  time: string
}
const history = ref<RunHistory[]>([])

// 选中的 workflow 信息
const selectedWf = computed(() =>
  wf.workflows.find((w) => w.id === selectedId.value),
)

async function onRun() {
  if (!selectedId.value) {
    runError.value = '请先选择 workflow'
    return
  }
  if (!taskInput.value.trim()) {
    runError.value = '请输入测试任务描述'
    return
  }
  running.value = true
  runError.value = ''
  runResult.value = null

  const wfName = selectedWf.value?.name || selectedId.value
  try {
    let res: unknown
    if (runMode.value === 'sequential') {
      res = await api.orchestrateWorkflow(selectedId.value, {
        task: taskInput.value,
      })
    } else {
      // parallel / vote / judge 都走 orchestrate-parallel，strategy 区分
      res = await api.runParallelWorkflow(selectedId.value, {
        task: taskInput.value,
        strategy: runMode.value,
      })
    }
    runResult.value = res
    history.value.unshift({
      workflowId: selectedId.value,
      workflowName: wfName,
      task: taskInput.value,
      mode: runMode.value,
      success: true,
      result: res,
      error: '',
      time: new Date().toLocaleString(),
    })
  } catch (e) {
    const msg = (e as Error).message
    runError.value = msg
    history.value.unshift({
      workflowId: selectedId.value,
      workflowName: wfName,
      task: taskInput.value,
      mode: runMode.value,
      success: false,
      result: null,
      error: msg,
      time: new Date().toLocaleString(),
    })
  } finally {
    running.value = false
  }
}

async function onCompile() {
  if (!selectedId.value) return
  await wf.loadFromServer(selectedId.value)
  await wf.loadCompiledConfig()
}

function onClearResult() {
  runResult.value = null
  runError.value = ''
}

onMounted(() => {
  wf.loadWorkflows()
})
</script>

<template>
  <div class="runner">
    <!-- 顶部：选择 workflow -->
    <div class="toolbar">
      <div class="left">
        <label class="lbl">Workflow:</label>
        <select v-model="selectedId" class="sel" @change="onCompile">
          <option value="">— 选择 workflow —</option>
          <option v-for="w in wf.workflows" :key="w.id" :value="w.id">
            {{ w.name }} v{{ w.active_version }}
            {{ w.is_deployed ? '✓部署' : '草稿' }}
          </option>
        </select>
      </div>
      <div class="right">
        <span v-if="selectedWf" class="meta">
          {{ selectedWf.name }}
          <span class="badge" :class="{ on: selectedWf.is_deployed }">
            {{ selectedWf.is_deployed ? '已部署' : '草稿' }}
          </span>
          v{{ selectedWf.active_version }}
        </span>
      </div>
    </div>

    <!-- 执行配置 -->
    <div class="config-panel">
      <div class="row">
        <label class="lbl">测试任务:</label>
        <input
          v-model="taskInput"
          placeholder="如：写一个 demo API 并测试"
          class="inp"
          @keydown.ctrl.enter="onRun"
        />
      </div>
      <div class="row">
        <label class="lbl">模式:</label>
        <label class="mode-opt">
          <input
            v-model="runMode"
            type="radio"
            value="sequential"
          />
          Sequential（串行）
        </label>
        <label class="mode-opt">
          <input
            v-model="runMode"
            type="radio"
            value="parallel"
          />
          Parallel（并行 fan-out）
        </label>
        <label class="mode-opt" title="多数投票合并（V3-T7）">
          <input
            v-model="runMode"
            type="radio"
            value="vote"
          />
          Vote（多数投票）
        </label>
        <label class="mode-opt" title="法官代理综合评判（V3-T7）">
          <input
            v-model="runMode"
            type="radio"
            value="judge"
          />
          Judge（法官代理）
        </label>
        <span class="spacer" />
        <button
          class="btn primary"
          :disabled="running || !selectedId || !taskInput.trim()"
          @click="onRun"
        >
          {{ running ? '执行中…' : '▶ 运行' }}
        </button>
        <button class="btn ghost" @click="onClearResult">清空</button>
      </div>
    </div>

    <!-- 编译产物预览 -->
    <div v-if="wf.compiledConfig" class="compile-section">
      <details>
        <summary>编译产物（deepagents 配置）</summary>
        <pre class="code">{{ JSON.stringify(wf.compiledConfig, null, 2) }}</pre>
      </details>
    </div>
    <div v-if="wf.compileError" class="err">{{ wf.compileError }}</div>

    <!-- 执行结果 -->
    <div class="result-section">
      <h4>执行结果</h4>
      <div v-if="runError" class="err-box">{{ runError }}</div>
      <pre v-else-if="runResult" class="code result">{{
        JSON.stringify(runResult, null, 2)
      }}</pre>
      <div v-else class="empty">选择 workflow 并输入任务后点击「运行」</div>
    </div>

    <!-- 历史运行 -->
    <div class="history-section">
      <h4>运行历史（{{ history.length }}）</h4>
      <div v-if="history.length === 0" class="empty">暂无</div>
      <div
        v-for="(h, i) in history"
        :key="i"
        class="hist-item"
        :class="{ fail: !h.success }"
      >
        <div class="hist-head">
          <span class="hist-name">{{ h.workflowName }}</span>
          <span class="hist-mode">{{ h.mode }}</span>
          <span class="hist-status" :class="h.success ? 'ok' : 'no'">
            {{ h.success ? '✓' : '✕' }}
          </span>
          <span class="hist-time">{{ h.time }}</span>
        </div>
        <div class="hist-task">{{ h.task }}</div>
        <pre v-if="h.result" class="code small">{{ JSON.stringify(h.result, null, 2) }}</pre>
        <div v-if="h.error" class="err-box small">{{ h.error }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.runner {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow-y: auto;
  padding: 16px;
  gap: 16px;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.toolbar .left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.lbl {
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}
.sel {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
  min-width: 200px;
}
.meta {
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
.config-panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.inp {
  flex: 1;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 8px;
  font-size: 13px;
}
.mode-opt {
  font-size: 12px;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
}
.spacer {
  flex: 1;
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
.compile-section details {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
}
.compile-section summary {
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
}
.result-section {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.result-section h4 {
  font-size: 12px;
  color: var(--muted);
  text-transform: uppercase;
  margin-bottom: 8px;
}
.err {
  color: #ff6b6b;
  font-size: 12px;
}
.err-box {
  color: #ff6b6b;
  font-size: 12px;
  background: rgba(255, 107, 107, 0.1);
  border: 1px solid rgba(255, 107, 107, 0.3);
  border-radius: 6px;
  padding: 8px;
}
.err-box.small {
  margin-top: 6px;
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
  max-height: 300px;
  overflow-y: auto;
  margin: 0;
}
.code.result {
  max-height: 400px;
}
.code.small {
  max-height: 120px;
  margin-top: 6px;
}
.empty {
  color: var(--muted);
  font-size: 12px;
}
.history-section {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.history-section h4 {
  font-size: 12px;
  color: var(--muted);
  text-transform: uppercase;
  margin-bottom: 8px;
}
.hist-item {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  margin-bottom: 8px;
  background: var(--bg);
}
.hist-item.fail {
  border-color: rgba(255, 107, 107, 0.3);
}
.hist-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.hist-name {
  font-weight: 600;
  font-size: 12px;
}
.hist-mode {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--border);
  color: var(--muted);
}
.hist-status.ok {
  color: #10b981;
}
.hist-status.no {
  color: #ff6b6b;
}
.hist-time {
  font-size: 10px;
  color: var(--muted);
  margin-left: auto;
}
.hist-task {
  font-size: 12px;
  color: var(--text);
  margin-bottom: 4px;
}
</style>
