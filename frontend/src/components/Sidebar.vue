<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useChatStore } from '@/stores/chat'
import * as api from '@/api'

type View = 'chat' | 'workflow'
const props = defineProps<{ view?: View }>()
const emit = defineEmits<{ switchView: [View] }>()

const chat = useChatStore()

// Skill 上传表单
const showSkillForm = ref(false)
const skillName = ref('')
const skillDesc = ref('')
const skillContent = ref('')

async function onAddSkill() {
  if (!skillName.value || !skillContent.value) {
    alert('名称和内容必填')
    return
  }
  try {
    await api.createSkill({
      name: skillName.value,
      description: skillDesc.value,
      content: skillContent.value,
    })
    skillName.value = ''
    skillDesc.value = ''
    skillContent.value = ''
    chat.loadSkills()
  } catch (e) {
    alert((e as Error).message)
  }
}

async function onDeleteSkill(name: string) {
  await api.deleteSkill(name)
  chat.loadSkills()
}

async function onAddMemory() {
  const text = memInput.value.trim()
  if (!text) return
  await api.addMemory(text)
  memInput.value = ''
  chat.loadMemories()
}

async function onDeleteMemory(key: string) {
  await api.deleteMemory(key)
  chat.loadMemories()
}

const memInput = ref('')

onMounted(() => {
  chat.loadAgents()
  chat.loadSkills()
  chat.loadMemories()
  chat.loadSessions()
})
</script>

<template>
  <aside>
    <div>
      <h1>AgentForge</h1>
      <div class="sub">deepagents + OpenSandbox + PostgreSQL</div>
      <div class="thread-info">
        thread: <span>{{ chat.threadId || '—' }}</span>
      </div>
    </div>

    <div class="card">
      <h3>子代理</h3>
      <div v-if="chat.subagents.length === 0" class="empty">加载中…</div>
      <div v-for="s in chat.subagents" :key="s.name" class="item">
        <span class="name">{{ s.name }}</span>
        <span class="desc"> · {{ s.description }}</span>
      </div>
    </div>

    <div class="card">
      <h3>Skills</h3>
      <div v-if="chat.skills.length === 0" class="empty">暂无 Skill</div>
      <div v-for="s in chat.skills" :key="s.name" class="skill-item">
        <span>
          <span class="badge" :class="{ tenant: !s.is_global }">
            {{ s.is_global ? 'global' : '租户' }}
          </span>
          {{ s.name }}
          <span class="desc"> · {{ s.description }}</span>
        </span>
        <span v-if="!s.is_global" class="del" @click="onDeleteSkill(s.name)">✕</span>
      </div>
      <details>
        <summary @click="showSkillForm = !showSkillForm">上传 Skill</summary>
        <textarea
          v-model="skillContent"
          placeholder="Skill 内容（Markdown）"
          class="skill-input"
        />
        <input v-model="skillName" placeholder="skill 名称" class="skill-input" />
        <input v-model="skillDesc" placeholder="一句话描述" class="skill-input" />
        <button class="full-btn" @click="onAddSkill">上传</button>
      </details>
    </div>

    <div class="card">
      <h3>长期记忆</h3>
      <div v-if="chat.memories.length === 0" class="empty">暂无记忆</div>
      <div v-for="m in chat.memories" :key="m.key" class="mem-item">
        <span>{{ m.text }}</span>
        <span class="del" @click="onDeleteMemory(m.key)">✕</span>
      </div>
      <div class="row">
        <input v-model="memInput" placeholder="手动添加记忆…" class="mem-input" />
        <button class="small" @click="onAddMemory">+</button>
      </div>
    </div>

    <div class="card">
      <h3>会话历史</h3>
      <div v-if="chat.sessions.length === 0" class="empty">暂无会话</div>
      <div
        v-for="s in chat.sessions"
        :key="s.id"
        class="mem-item clickable"
        @click="chat.switchThread(s.thread_id)"
      >
        <span>
          {{ s.title || s.thread_id }}
          <br />
          <span class="ts">{{ new Date(s.last_active_at).toLocaleString() }}</span>
        </span>
      </div>
    </div>

    <div class="row">
      <button class="ghost" @click="chat.resetThread()">新建会话</button>
      <button
        class="ghost"
        :disabled="!chat.threadId"
        @click="chat.threadId && api.destroySandbox(chat.threadId)"
      >
        销毁沙箱
      </button>
    </div>

    <div class="row view-switch">
      <button
        class="ghost"
        :class="{ on: props.view === 'chat' }"
        @click="emit('switchView', 'chat')"
      >
        对话
      </button>
      <button
        class="ghost"
        :class="{ on: props.view === 'workflow' }"
        @click="emit('switchView', 'workflow')"
      >
        画布
      </button>
    </div>
  </aside>
</template>

<style scoped>
aside {
  width: 280px;
  background: var(--panel);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 16px;
  gap: 16px;
  overflow-y: auto;
}
h1 {
  font-size: 16px;
  margin-bottom: 4px;
}
.sub {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 8px;
}
.thread-info {
  font-size: 12px;
  color: var(--muted);
}
.card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
}
h3 {
  font-size: 13px;
  color: var(--muted);
  margin-bottom: 8px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.empty {
  color: var(--muted);
  font-size: 12px;
}
.item {
  font-size: 13px;
  margin-bottom: 6px;
}
.item .name {
  color: var(--warn);
  font-weight: 600;
}
.item .desc,
.skill-item .desc {
  color: var(--muted);
  font-size: 12px;
}
.skill-item,
.mem-item {
  font-size: 12px;
  padding: 6px 8px;
  background: var(--panel);
  border-radius: 6px;
  margin-bottom: 6px;
  display: flex;
  justify-content: space-between;
  gap: 6px;
}
.skill-item .badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--accent);
  color: #fff;
}
.skill-item .badge.tenant {
  background: var(--warn);
}
.del {
  color: var(--muted);
  cursor: pointer;
}
.del:hover {
  color: #ff6b6b;
}
.mem-item.clickable {
  cursor: pointer;
}
.mem-item .ts {
  color: var(--muted);
  font-size: 10px;
}
summary {
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
  margin-top: 8px;
}
.skill-input {
  margin-top: 6px;
  width: 100%;
}
.full-btn {
  margin-top: 6px;
  padding: 6px 10px;
  font-size: 12px;
  width: 100%;
}
.row {
  display: flex;
  gap: 8px;
}
.row button {
  flex: 1;
  padding: 8px 10px;
  font-size: 12px;
}
.mem-input {
  flex: 1;
  background: var(--panel);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
}
.small {
  padding: 6px 10px;
  font-size: 12px;
}
.view-switch button.on {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}</style>
