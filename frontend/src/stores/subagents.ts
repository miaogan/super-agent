// V2.5 子代理 store：管理自定义 + 内置镜像子代理，供 SubAgentManager 和 WorkflowBuilder 复用

import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as api from '@/api'
import type {
  SubAgentCreateRequest,
  SubAgentItem,
  SubAgentUpdateRequest,
} from '@/types'

export const useSubAgentsStore = defineStore('subagents', () => {
  const items = ref<SubAgentItem[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function load() {
    loading.value = true
    error.value = null
    try {
      const d = await api.listSubAgents()
      items.value = d.items
    } catch (e) {
      error.value = (e as Error).message
      console.error('加载子代理失败', e)
    } finally {
      loading.value = false
    }
  }

  async function create(req: SubAgentCreateRequest): Promise<SubAgentItem> {
    const item = await api.createSubAgent(req)
    items.value.push(item)
    return item
  }

  async function update(id: string, req: SubAgentUpdateRequest): Promise<SubAgentItem> {
    const item = await api.updateSubAgent(id, req)
    const i = items.value.findIndex((x) => x.id === id)
    if (i >= 0) items.value[i] = item
    return item
  }

  async function remove(id: string): Promise<void> {
    await api.deleteSubAgent(id)
    items.value = items.value.filter((x) => x.id !== id)
  }

  async function fork(id: string, newName: string): Promise<SubAgentItem> {
    const item = await api.forkSubAgent(id, newName)
    items.value.push(item)
    return item
  }

  /** 按 name 查找（用于 workflow 节点引用解析） */
  function byName(name: string): SubAgentItem | undefined {
    return items.value.find((x) => x.name === name)
  }

  return {
    items,
    loading,
    error,
    load,
    create,
    update,
    remove,
    fork,
    byName,
  }
})
