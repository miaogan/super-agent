<script setup lang="ts">
// V2 画布主体：Vue Flow + 节点面板 + 连线校验 + 属性面板

import { onMounted, computed, markRaw } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'

import BaseNode from './BaseNode.vue'
import NodePropsPanel from './NodePropsPanel.vue'
import { useWorkflowStore, NODE_PALETTE } from '@/stores/workflow'
import type { WorkflowNodeType } from '@/types'

const wf = useWorkflowStore()
const { onConnect, addEdges, onNodeDragStop, onNodeClick, onPaneClick, onEdgeClick } =
  useVueFlow()

// Vue Flow 需要把节点 type 映射到组件（markRaw 避免 reactivity 包装）
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const nodeTypes = markRaw({
  start: BaseNode,
  agent: BaseNode,
  tool: BaseNode,
  subagent: BaseNode,
  end: BaseNode,
}) as any

// 连线校验：start 不能有入边；end 不能有出边
function isValidConnection(sourceId: string, targetId: string): boolean {
  const src = wf.nodes.find((n) => n.id === sourceId)
  const tgt = wf.nodes.find((n) => n.id === targetId)
  if (src?.type === 'end') return false
  if (tgt?.type === 'start') return false
  return true
}

onConnect((params) => {
  if (!isValidConnection(params.source, params.target)) return
  const id = `e_${params.source}_${params.target}_${Date.now().toString(36)}`
  wf.addEdge({ id, source: params.source, target: params.target })
  addEdges([{ id, source: params.source, target: params.target }])
})

onNodeDragStop(({ node }) => {
  if (node.position) {
    wf.updateNodePosition(node.id, { x: node.position.x, y: node.position.y })
  }
})

onNodeClick(({ node }) => {
  wf.selectNode(node.id)
})

onPaneClick(() => {
  wf.selectNode(null)
})

onEdgeClick(({ edge }) => {
  if (confirm('删除这条连线？')) {
    wf.removeEdge(edge.id)
  }
})

// 节点面板拖拽到画布
function onDragStart(e: DragEvent, type: WorkflowNodeType) {
  if (!e.dataTransfer) return
  e.dataTransfer.setData('application/wf-node', type)
  e.dataTransfer.effectAllowed = 'move'
}

function onDrop(e: DragEvent) {
  e.preventDefault()
  const type = e.dataTransfer?.getData('application/wf-node') as WorkflowNodeType
  if (!type) return
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  wf.addNode(type, {
    x: e.clientX - rect.left - 70,
    y: e.clientY - rect.top - 20,
  })
}

function onDragOver(e: DragEvent) {
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
}

// 键盘删除选中节点
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Delete' || e.key === 'Backspace') {
    const target = e.target as HTMLElement
    if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') return
    if (wf.selectedNodeId) {
      e.preventDefault()
      wf.removeNode(wf.selectedNodeId)
    }
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
})

// 画布响应式同步：把 store 的 nodes/edges 转成 Vue Flow 格式
const vfNodes = computed(() =>
  wf.nodes.map((n) => ({
    id: n.id,
    type: n.type,
    position: n.position,
    data: { ...n.data, selected: wf.selectedNodeId === n.id },
  })),
)
const vfEdges = computed(() =>
  wf.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    animated: true,
  })),
)
</script>

<template>
  <div class="wf-canvas-wrap">
    <!-- 左侧节点面板 -->
    <div class="palette">
      <h3>节点</h3>
      <div
        v-for="n in NODE_PALETTE"
        :key="n.type"
        class="palette-item"
        :style="{ '--c': n.color }"
        draggable="true"
        @dragstart="onDragStart($event, n.type)"
      >
        <span class="dot" />
        <div>
          <div class="title">{{ n.label }}</div>
          <div class="desc">{{ n.description }}</div>
        </div>
      </div>
    </div>

    <!-- 中间画布 -->
    <div class="canvas" @drop="onDrop" @dragover="onDragOver">
      <VueFlow
        :nodes="vfNodes"
        :edges="vfEdges"
        :node-types="nodeTypes"
        :default-viewport="{ zoom: 1 }"
        :min-zoom="0.2"
        :max-zoom="2"
        fit-view-on-init
      >
        <Background pattern-color="#3a3a4a" :gap="20" />
        <Controls />
        <MiniMap />
      </VueFlow>
    </div>

    <!-- 右侧属性面板 -->
    <div class="props-panel">
      <NodePropsPanel />
    </div>
  </div>
</template>

<style scoped>
.wf-canvas-wrap {
  display: flex;
  height: 100%;
  width: 100%;
}
.palette {
  width: 200px;
  background: var(--panel);
  border-right: 1px solid var(--border);
  padding: 12px;
  overflow-y: auto;
  flex-shrink: 0;
}
.palette h3 {
  font-size: 12px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}
.palette-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--c);
  border-radius: 8px;
  margin-bottom: 6px;
  cursor: grab;
  background: var(--bg);
  transition: transform 0.1s, box-shadow 0.1s;
}
.palette-item:hover {
  transform: translateX(2px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}
.palette-item:active {
  cursor: grabbing;
}
.palette-item .dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--c);
  margin-top: 3px;
  flex-shrink: 0;
}
.palette-item .title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.palette-item .desc {
  font-size: 11px;
  color: var(--muted);
  margin-top: 2px;
}
.canvas {
  flex: 1;
  position: relative;
  background: var(--bg);
  min-width: 0;
}
.props-panel {
  width: 280px;
  background: var(--panel);
  border-left: 1px solid var(--border);
  padding: 12px;
  overflow-y: auto;
  flex-shrink: 0;
}
</style>
