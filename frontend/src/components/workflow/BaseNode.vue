<script setup lang="ts">
// 画布通用节点：按 type 着色 + 显示标题；可拖拽 + 选中高亮

import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'

const props = defineProps<{
  id: string
  type: string
  data: Record<string, unknown>
  selected?: boolean
}>()

const meta = computed(() => {
  const map: Record<string, { label: string; color: string; sub: string }> = {
    start: { label: 'Start', color: '#10b981', sub: '入口' },
    agent: { label: 'Agent', color: '#3b82f6', sub: 'LLM 节点' },
    tool: { label: 'Tool', color: '#f59e0b', sub: '工具节点' },
    subagent: { label: 'Subagent', color: '#8b5cf6', sub: '子代理编排' },
    end: { label: 'End', color: '#ef4444', sub: '出口' },
  }
  return map[props.type] ?? { label: props.type, color: '#64748b', sub: '' }
})

const subtitle = computed(() => {
  const d = props.data as {
    system_prompt?: string
    tool?: string
    subagents?: unknown[]
  }
  switch (props.type) {
    case 'start':
      return d.system_prompt ? d.system_prompt.slice(0, 30) : '用户消息入口'
    case 'agent':
      return d.system_prompt ? d.system_prompt.slice(0, 30) : '(空 prompt)'
    case 'tool':
      return d.tool || '(未选工具)'
    case 'subagent': {
      const n = d.subagents?.length ?? 0
      return n ? `${n} 个子代理` : '(空子代理)'
    }
    case 'end':
      return '返回最终回复'
    default:
      return ''
  }
})
</script>

<template>
  <div class="wf-node" :class="meta.label.toLowerCase(), { sel: selected }" :style="{ '--c': meta.color }">
    <Handle type="target" :position="Position.Left" />
    <div class="head">
      <span class="dot" />
      <span class="title">{{ meta.label }}</span>
    </div>
    <div class="sub">{{ subtitle }}</div>
    <Handle type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
.wf-node {
  min-width: 140px;
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--panel);
  border: 2px solid var(--c);
  font-size: 12px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  cursor: grab;
  transition: box-shadow 0.15s;
}
.wf-node.sel {
  box-shadow: 0 0 0 3px var(--c), 0 4px 12px rgba(0, 0, 0, 0.25);
}
.wf-node:active {
  cursor: grabbing;
}
.head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--c);
}
.title {
  font-weight: 600;
  color: var(--c);
}
.sub {
  color: var(--muted);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 160px;
}
</style>
