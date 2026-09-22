import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '@/api'
import type { ChatMessage, SkillItem, MemoryItem, SessionItem, SseEvent } from '@/types'

export const useChatStore = defineStore('chat', () => {
  const threadId = ref<string | null>(null)
  const streaming = ref(false)
  const messages = ref<ChatMessage[]>([])
  const skills = ref<SkillItem[]>([])
  const memories = ref<MemoryItem[]>([])
  const sessions = ref<SessionItem[]>([])
  const subagents = ref<{ name: string; description: string }[]>([])

  const lastActiveThread = computed(() => threadId.value)

  function addMessage(m: ChatMessage) {
    messages.value.push(m)
  }

  function resetThread() {
    threadId.value = null
    messages.value = []
  }

  async function loadAgents() {
    try {
      const d = await api.listAgents()
      subagents.value = d.subagents.map((s) => ({ name: s.name, description: s.description }))
    } catch (e) {
      console.error('加载子代理失败', e)
    }
  }

  async function loadSkills() {
    try {
      const d = await api.listSkills()
      skills.value = d.items
    } catch (e) {
      console.error('加载 Skills 失败', e)
    }
  }

  async function loadMemories() {
    try {
      const d = await api.listMemories()
      memories.value = d.items
    } catch (e) {
      console.error('加载记忆失败', e)
    }
  }

  async function loadSessions() {
    try {
      const d = await api.listSessions()
      sessions.value = d.items
    } catch (e) {
      console.error('加载会话列表失败', e)
    }
  }

  async function switchThread(t: string) {
    threadId.value = t
    messages.value = []
    try {
      const d = await api.getSessionHistory(t)
      for (const m of d.messages) {
        messages.value.push({
          role: m.role === 'human' ? 'user' : 'ai',
          content: m.content,
        })
      }
    } catch (e) {
      // 忽略，可能为空
    }
  }

  async function sendChat(text: string) {
    if (streaming.value) return
    streaming.value = true
    addMessage({ role: 'user', content: text })
    let aiBuf = ''
    let aiStarted = false

    try {
      await api.streamChat(text, threadId.value, (e: SseEvent) => {
        switch (e.event) {
          case 'start':
            threadId.value = e.data.thread_id
            break
          case 'token':
            if (!aiStarted) {
              aiStarted = true
              addMessage({ role: 'ai', content: '' })
            }
            aiBuf += e.data.content
            messages.value[messages.value.length - 1].content = aiBuf
            break
          case 'tool_start':
            addMessage({
              role: 'event',
              content: `🔧 ${e.data.tool} ${JSON.stringify(e.data.args).slice(0, 120)}`,
            })
            break
          case 'tool_end':
            addMessage({
              role: 'event',
              content: `✅ ${e.data.tool}：${e.data.result.slice(0, 140)}`,
            })
            break
          case 'subagent_start':
            addMessage({
              role: 'event',
              subtype: 'subagent',
              content: `🤖 子代理 ${e.data.agent} 启动：${e.data.description}`,
            })
            break
          case 'subagent_end':
            addMessage({
              role: 'event',
              subtype: 'subagent',
              content: `🤖 子代理完成：${e.data.report.slice(0, 160)}`,
            })
            break
          case 'memory':
            addMessage({
              role: 'event',
              subtype: 'memory',
              content: `🧠 长期记忆 ${e.data.text}`,
            })
            loadMemories()
            break
          case 'citation':
            // V2.5-T5：retrieve_knowledge 返回，渲染引用块
            addMessage({
              role: 'citation',
              content: '',
              citations: e.data.citation,
              contexts: e.data.contexts,
              ragMode: e.data.mode,
              ragError: e.data.error ?? null,
            })
            break
          case 'done':
            if (aiStarted && !aiBuf) {
              messages.value[messages.value.length - 1].content = e.data.content
            } else if (!aiStarted) {
              addMessage({ role: 'ai', content: e.data.content })
            }
            break
          case 'error':
            addMessage({ role: 'event', content: `⚠️ 错误 ${e.data.message}` })
            break
        }
      })
      loadSessions()
    } catch (e) {
      addMessage({ role: 'event', content: `⚠️ 连接错误 ${String(e)}` })
    } finally {
      streaming.value = false
    }
  }

  return {
    threadId,
    streaming,
    messages,
    skills,
    memories,
    sessions,
    subagents,
    lastActiveThread,
    addMessage,
    resetThread,
    loadAgents,
    loadSkills,
    loadMemories,
    loadSessions,
    switchThread,
    sendChat,
  }
})
