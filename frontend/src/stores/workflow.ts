// V2 Workflow 画布 store：管理画布节点/连线 + CRUD + 编译预览

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type {
  CompiledConfig,
  WorkflowDefinition,
  WorkflowEdge,
  WorkflowItem,
  WorkflowNode,
  WorkflowNodeType,
  WorkflowSubagentSpec,
  OrchestratorRunResponse,
} from '@/types'
import * as api from '@/api'

/** 节点类型元信息（用于侧边栏节点面板） */
export interface NodeTypeInfo {
  type: WorkflowNodeType
  label: string
  color: string
  description: string
}

export const NODE_PALETTE: NodeTypeInfo[] = [
  {
    type: 'start',
    label: 'Start',
    color: '#10b981',
    description: '入口节点：接收用户消息',
  },
  {
    type: 'agent',
    label: 'Agent',
    color: '#3b82f6',
    description: 'LLM 节点：system_prompt + model + tools 子集',
  },
  {
    type: 'tool',
    label: 'Tool',
    color: '#f59e0b',
    description: '工具节点：sandbox / memory / search 等',
  },
  {
    type: 'subagent',
    label: 'Subagent',
    color: '#8b5cf6',
    description: '子代理节点：sequential 串行编排',
  },
  {
    type: 'end',
    label: 'End',
    color: '#ef4444',
    description: '出口节点：返回最终回复',
  },
]

let _nodeSeq = 0
function genNodeId(type: WorkflowNodeType): string {
  _nodeSeq += 1
  return `${type}_${Date.now().toString(36)}_${_nodeSeq}`
}

/** 初始画布：start → end 单链 */
function defaultCanvas(): { nodes: WorkflowNode[]; edges: WorkflowEdge[] } {
  return {
    nodes: [
      {
        id: 'start_1',
        type: 'start',
        data: { system_prompt: '你是一个有用的助手。' },
        position: { x: 80, y: 200 },
      },
      {
        id: 'end_1',
        type: 'end',
        data: {},
        position: { x: 640, y: 200 },
      },
    ],
    edges: [{ id: 'e_start_end', source: 'start_1', target: 'end_1' }],
  }
}

export const useWorkflowStore = defineStore('workflow', () => {
  // 当前编辑的画布
  const nodes = ref<WorkflowNode[]>(defaultCanvas().nodes)
  const edges = ref<WorkflowEdge[]>(defaultCanvas().edges)

  // 已保存的 workflow 列表
  const workflows = ref<WorkflowItem[]>([])
  const currentWorkflowId = ref<string | null>(null)
  const currentWorkflow = ref<WorkflowItem | null>(null)

  // 编译预览产物
  const compiledConfig = ref<CompiledConfig | null>(null)
  const compileError = ref<string | null>(null)
  const compiling = ref(false)

  // 编排结果
  const orchestrateResult = ref<OrchestratorRunResponse | null>(null)
  const orchestrating = ref(false)

  // 选中节点（用于右侧属性面板）
  const selectedNodeId = ref<string | null>(null)
  const selectedNode = computed<WorkflowNode | null>(
    () => nodes.value.find((n) => n.id === selectedNodeId.value) ?? null,
  )

  // ----- 画布操作 -----

  function addNode(type: WorkflowNodeType, position: { x: number; y: number }) {
    const id = genNodeId(type)
    const data = type === 'subagent' ? { subagents: [] as WorkflowSubagentSpec[] } : {}
    const node: WorkflowNode = { id, type, data, position }
    nodes.value = [...nodes.value, node]
    selectedNodeId.value = id
    return id
  }

  function updateNodeData(id: string, data: Partial<WorkflowNode['data']>) {
    nodes.value = nodes.value.map((n) =>
      n.id === id ? { ...n, data: { ...n.data, ...data } } : n,
    )
  }

  function removeNode(id: string) {
    nodes.value = nodes.value.filter((n) => n.id !== id)
    edges.value = edges.value.filter((e) => e.source !== id && e.target !== id)
    if (selectedNodeId.value === id) selectedNodeId.value = null
  }

  function addEdge(edge: WorkflowEdge) {
    if (edges.value.some((e) => e.id === edge.id)) return
    edges.value = [...edges.value, edge]
  }

  function removeEdge(id: string) {
    edges.value = edges.value.filter((e) => e.id !== id)
  }

  function updateNodePosition(id: string, position: { x: number; y: number }) {
    nodes.value = nodes.value.map((n) =>
      n.id === id ? { ...n, position } : n,
    )
  }

  function selectNode(id: string | null) {
    selectedNodeId.value = id
  }

  function clearCanvas() {
    const init = defaultCanvas()
    nodes.value = init.nodes
    edges.value = init.edges
    selectedNodeId.value = null
    compiledConfig.value = null
    compileError.value = null
    currentWorkflowId.value = null
    currentWorkflow.value = null
  }

  /** 导出为后端编译器接受的 definition（去掉 Vue Flow 内部字段） */
  function exportDefinition(): WorkflowDefinition {
    return {
      nodes: nodes.value.map((n) => ({
        id: n.id,
        type: n.type,
        data: { ...n.data },
        position: { ...n.position },
      })),
      edges: edges.value.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
      })),
    }
  }

  /** 从 definition JSON 加载到画布 */
  function loadDefinition(def: WorkflowDefinition) {
    nodes.value = def.nodes.map((n) => ({
      id: n.id,
      type: n.type,
      data: { ...n.data },
      position: { x: n.position.x, y: n.position.y },
    }))
    edges.value = def.edges.map((e) => ({ ...e }))
    selectedNodeId.value = null
  }

  // ----- API 交互 -----

  async function loadWorkflows() {
    try {
      const res = await api.listWorkflows()
      workflows.value = res.items
    } catch (e) {
      console.error('加载 workflow 列表失败', e)
    }
  }

  async function saveAs(name: string, description: string): Promise<WorkflowItem | null> {
    try {
      const wf = await api.createWorkflow({
        name,
        description,
        definition: exportDefinition(),
      })
      workflows.value = [wf, ...workflows.value.filter((w) => w.id !== wf.id)]
      currentWorkflowId.value = wf.id
      currentWorkflow.value = wf
      return wf
    } catch (e) {
      alert((e as Error).message)
      return null
    }
  }

  async function saveUpdate(): Promise<WorkflowItem | null> {
    if (!currentWorkflowId.value) return null
    try {
      const wf = await api.updateWorkflow(currentWorkflowId.value, {
        definition: exportDefinition(),
      })
      currentWorkflow.value = wf
      workflows.value = workflows.value.map((w) => (w.id === wf.id ? wf : w))
      return wf
    } catch (e) {
      alert((e as Error).message)
      return null
    }
  }

  async function deploy(): Promise<WorkflowItem | null> {
    if (!currentWorkflowId.value) return null
    try {
      const wf = await api.updateWorkflow(currentWorkflowId.value, {
        definition: exportDefinition(),
        is_deployed: true,
      })
      currentWorkflow.value = wf
      workflows.value = workflows.value.map((w) => (w.id === wf.id ? wf : w))
      return wf
    } catch (e) {
      alert((e as Error).message)
      return null
    }
  }

  async function remove(id: string) {
    try {
      await api.deleteWorkflow(id)
      workflows.value = workflows.value.filter((w) => w.id !== id)
      if (currentWorkflowId.value === id) clearCanvas()
    } catch (e) {
      alert((e as Error).message)
    }
  }

  async function loadFromServer(id: string) {
    try {
      const wf = await api.getWorkflow(id)
      currentWorkflowId.value = wf.id
      currentWorkflow.value = wf
      const versions = await api.listWorkflowVersions(id)
      const active = versions.items.find((v) => v.version === wf.active_version)
      if (active) {
        const def = JSON.parse(active.definition) as WorkflowDefinition
        loadDefinition(def)
      }
    } catch (e) {
      alert((e as Error).message)
    }
  }

  /** 实时编译预览（不落库） */
  async function previewCompile() {
    compiling.value = true
    compileError.value = null
    try {
      const res = await api.compilePreview({ definition: exportDefinition() })
      compiledConfig.value = res.config
    } catch (e) {
      compileError.value = (e as Error).message
      compiledConfig.value = null
    } finally {
      compiling.value = false
    }
  }

  /** 读取已落库 workflow 的编译产物 */
  async function loadCompiledConfig() {
    if (!currentWorkflowId.value) return
    try {
      const res = await api.getCompiledConfig(currentWorkflowId.value)
      compiledConfig.value = res.config
    } catch (e) {
      console.error('加载编译产物失败', e)
    }
  }

  /** sequential 编排执行 */
  async function runOrchestrate(task: string): Promise<OrchestratorRunResponse | null> {
    if (!currentWorkflowId.value) {
      alert('请先保存 workflow')
      return null
    }
    orchestrating.value = true
    try {
      const res = await api.orchestrateWorkflow(currentWorkflowId.value, { task })
      orchestrateResult.value = res
      return res
    } catch (e) {
      alert((e as Error).message)
      return null
    } finally {
      orchestrating.value = false
    }
  }

  return {
    // state
    nodes,
    edges,
    workflows,
    currentWorkflowId,
    currentWorkflow,
    compiledConfig,
    compileError,
    compiling,
    orchestrateResult,
    orchestrating,
    selectedNodeId,
    selectedNode,
    // 画布操作
    addNode,
    updateNodeData,
    removeNode,
    addEdge,
    removeEdge,
    updateNodePosition,
    selectNode,
    clearCanvas,
    exportDefinition,
    loadDefinition,
    // API
    loadWorkflows,
    saveAs,
    saveUpdate,
    deploy,
    remove,
    loadFromServer,
    previewCompile,
    loadCompiledConfig,
    runOrchestrate,
  }
})
