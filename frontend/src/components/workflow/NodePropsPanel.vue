<script setup lang="ts">
// 节点属性面板：按 type 显示不同字段编辑器

import { computed } from 'vue'
import { useWorkflowStore } from '@/stores/workflow'

const wf = useWorkflowStore()
const node = computed(() => wf.selectedNode)
const subs = computed(() => node.value?.data.subagents || [])

function updateSub(i: number, patch: Partial<{ name: string; description: string }>) {
  if (!node.value) return
  const next = [...subs.value]
  next[i] = { ...next[i], ...patch }
  wf.updateNodeData(node.value.id, { subagents: next })
}
function addSub() {
  if (!node.value) return
  const next = [
    ...subs.value,
    { name: `agent_${subs.value.length + 1}`, description: '' },
  ]
  wf.updateNodeData(node.value.id, { subagents: next })
}
function removeSub(i: number) {
  if (!node.value) return
  const next = subs.value.filter((_, j) => j !== i)
  wf.updateNodeData(node.value.id, { subagents: next })
}
</script>

<template>
  <div v-if="node" class="props">
    <h3>{{ node.type }} 节点</h3>
    <div class="field-id">id: {{ node.id }}</div>

    <!-- system_prompt（start / agent） -->
    <template v-if="node.type === 'start' || node.type === 'agent'">
      <label class="lbl">System Prompt</label>
      <textarea
        class="ta"
        :value="node.data.system_prompt || ''"
        @input="wf.updateNodeData(node!.id, { system_prompt: ($event.target as HTMLTextAreaElement).value })"
        placeholder="系统提示词"
      />
    </template>

    <!-- agent 节点：model / task_type / tools / skills -->
    <template v-if="node.type === 'agent'">
      <label class="lbl">Model</label>
      <input
        class="inp"
        :value="node.data.model || ''"
        @input="wf.updateNodeData(node!.id, { model: ($event.target as HTMLInputElement).value })"
        placeholder="auto / openai:gpt-4o-mini"
      />

      <label class="lbl">Task Type（模型路由）</label>
      <select
        class="inp"
        :value="node.data.task_type || ''"
        @change="wf.updateNodeData(node!.id, { task_type: ($event.target as HTMLSelectElement).value })"
      >
        <option value="">auto</option>
        <option value="coding">coding</option>
        <option value="review">review</option>
        <option value="summary">summary</option>
        <option value="chat">chat</option>
        <option value="reasoning">reasoning</option>
      </select>

      <label class="lbl">Tools（逗号分隔）</label>
      <input
        class="inp"
        :value="(node.data.tools || []).join(',')"
        @input="wf.updateNodeData(node!.id, { tools: ($event.target as HTMLInputElement).value.split(',').map((s) => s.trim()).filter(Boolean) })"
        placeholder="sandbox,memory,search"
      />

      <label class="lbl">Skills（逗号分隔）</label>
      <input
        class="inp"
        :value="(node.data.skills || []).join(',')"
        @input="wf.updateNodeData(node!.id, { skills: ($event.target as HTMLInputElement).value.split(',').map((s) => s.trim()).filter(Boolean) })"
        placeholder="greet,summarize"
      />
    </template>

    <!-- tool 节点 -->
    <template v-if="node.type === 'tool'">
      <label class="lbl">Tool Name</label>
      <select
        class="inp"
        :value="node.data.tool || ''"
        @change="wf.updateNodeData(node!.id, { tool: ($event.target as HTMLSelectElement).value })"
      >
        <option value="">(请选择)</option>
        <option value="sandbox">sandbox</option>
        <option value="memory">memory</option>
        <option value="search">search</option>
        <option value="task">task</option>
      </select>
    </template>

    <!-- subagent 节点 -->
    <template v-if="node.type === 'subagent'">
      <label class="lbl">子代理列表（{{ subs.length }}）</label>
      <div v-for="(sub, i) in subs" :key="i" class="sub-row">
        <input
          class="inp"
          :value="sub.name"
          @input="updateSub(i, { name: ($event.target as HTMLInputElement).value })"
          placeholder="name"
        />
        <input
          class="inp"
          :value="sub.description || ''"
          @input="updateSub(i, { description: ($event.target as HTMLInputElement).value })"
          placeholder="description"
        />
        <button class="btn-del" @click="removeSub(i)">✕</button>
      </div>
      <button class="btn-add" @click="addSub()">+ 添加子代理</button>
    </template>

    <button class="btn-remove" @click="wf.removeNode(node!.id)">删除节点</button>
  </div>
  <div v-else class="empty">点击节点编辑属性</div>
</template>

<style scoped>
.props {
  font-size: 12px;
}
.empty {
  color: var(--muted);
  font-size: 13px;
  text-align: center;
  margin-top: 40px;
}
.props h3 {
  font-size: 14px;
  margin-bottom: 8px;
}
.field-id {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 12px;
  font-family: monospace;
}
.lbl {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin: 8px 0 4px;
}
.ta {
  width: 100%;
  min-height: 60px;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
  resize: vertical;
  box-sizing: border-box;
}
.inp {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
  box-sizing: border-box;
}
.sub-row {
  display: flex;
  gap: 4px;
  margin-bottom: 4px;
}
.sub-row .inp {
  flex: 1;
  min-width: 0;
}
.btn-del {
  background: transparent;
  border: 1px solid var(--border);
  color: #ff6b6b;
  border-radius: 4px;
  padding: 2px 6px;
  cursor: pointer;
  font-size: 11px;
  flex-shrink: 0;
}
.btn-add {
  width: 100%;
  padding: 6px;
  background: var(--bg);
  border: 1px dashed var(--border);
  color: var(--muted);
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
  margin-top: 4px;
  box-sizing: border-box;
}
.btn-add:hover {
  color: var(--text);
  border-color: var(--accent);
}
.btn-remove {
  width: 100%;
  margin-top: 16px;
  padding: 8px;
  background: transparent;
  border: 1px solid #ff6b6b;
  color: #ff6b6b;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}
.btn-remove:hover {
  background: #ff6b6b;
  color: #fff;
}
</style>
