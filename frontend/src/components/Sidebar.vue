<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useSubAgentsStore } from '@/stores/subagents'
import * as api from '@/api'

type View = 'chat' | 'workflow' | 'run' | 'agents' | 'market'
const props = defineProps<{ view?: View }>()
const emit = defineEmits<{ switchView: [View] }>()

const chat = useChatStore()
const subagents = useSubAgentsStore()

// Skill 上传表单
const showSkillForm = ref(false)
const skillName = ref('')
const skillDesc = ref('')
const skillContent = ref('')
// 压缩包上传
const archiveFile = ref<File | null>(null)
const uploadingArchive = ref(false)

function onArchiveChange(e: Event) {
  const target = e.target as HTMLInputElement
  if (target.files && target.files[0]) {
    archiveFile.value = target.files[0]
  }
}

async function onUploadArchive() {
  if (!archiveFile.value) {
    alert('请选择 zip 文件')
    return
  }
  if (!skillName.value) {
    alert('请填写 skill 名称')
    return
  }
  uploadingArchive.value = true
  try {
    await api.uploadSkillArchive(
      skillName.value,
      skillDesc.value,
      archiveFile.value,
    )
    archiveFile.value = null
    skillName.value = ''
    skillDesc.value = ''
    chat.loadSkills()
    alert('压缩包上传成功')
  } catch (e) {
    alert((e as Error).message)
  } finally {
    uploadingArchive.value = false
  }
}

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
  chat.loadSkills()
  chat.loadMemories()
  chat.loadSessions()
  subagents.load()
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

    <div class="card clickable" @click="emit('switchView', 'agents')">
      <h3>子代理（{{ subagents.items.length }}）</h3>
      <div v-if="subagents.items.length === 0" class="empty">点击管理 →</div>
      <div v-for="s in subagents.items.slice(0, 5)" :key="s.id" class="item">
        <span class="badge" :class="{ custom: !s.is_builtin }">
          {{ s.is_builtin ? '内置' : '自定义' }}
        </span>
        <span class="name">{{ s.name }}</span>
      </div>
      <div class="hint-link">管理 / Fork / 复用到 workflow →</div>
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
        <!-- 单文件模式 -->
        <div class="upload-section">
          <div class="upload-label">单文件 Skill</div>
          <textarea
            v-model="skillContent"
            placeholder="Skill 内容（Markdown）"
            class="skill-input"
          />
          <input v-model="skillName" placeholder="skill 名称" class="skill-input" />
          <input v-model="skillDesc" placeholder="一句话描述" class="skill-input" />
          <button class="full-btn" @click="onAddSkill">上传单文件</button>
        </div>
        <!-- 压缩包模式 -->
        <div class="upload-section">
          <div class="upload-label">压缩包 Skill（多文件）</div>
          <input v-model="skillName" placeholder="skill 名称" class="skill-input" />
          <input v-model="skillDesc" placeholder="一句话描述" class="skill-input" />
          <input
            type="file"
            accept=".zip"
            class="skill-input"
            @change="onArchiveChange"
          />
          <button
            class="full-btn"
            :disabled="uploadingArchive"
            @click="onUploadArchive"
          >
            {{ uploadingArchive ? '上传中…' : '上传压缩包' }}
          </button>
          <div class="hint">zip 必须含 SKILL.md，可含脚本/资源</div>
        </div>
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
.card.clickable {
  cursor: pointer;
  transition: border-color 0.15s;
}
.card.clickable:hover {
  border-color: var(--accent);
}
.hint-link {
  margin-top: 8px;
  font-size: 11px;
  color: var(--accent);
  text-align: right;
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
.upload-section {
  margin-top: 8px;
  padding: 6px;
  border: 1px dashed var(--border);
  border-radius: 6px;
}
.upload-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  margin-bottom: 4px;
}
.hint {
  font-size: 10px;
  color: var(--muted);
  margin-top: 4px;
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
</style>
