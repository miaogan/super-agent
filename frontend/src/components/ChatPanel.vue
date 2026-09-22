<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { useChatStore } from '@/stores/chat'
import ChatMessage from './ChatMessage.vue'

const chat = useChatStore()
const input = ref('')

async function onSubmit() {
  const text = input.value.trim()
  if (!text || chat.streaming) return
  input.value = ''
  await chat.sendChat(text)
  await nextTick(scrollToBottom)
}

function scrollToBottom() {
  const el = document.getElementById('messages')
  if (el) el.scrollTop = el.scrollHeight
}

// 监听 messages 变化时滚动到底（token 累加）
import { watch } from 'vue'
watch(
  () => chat.messages.length,
  () => nextTick(scrollToBottom),
)
watch(
  () => chat.messages.map((m) => m.content).join(''),
  () => nextTick(scrollToBottom),
)
</script>

<template>
  <section>
    <div id="messages">
      <div v-if="chat.messages.length === 0" class="empty">输入消息开始对话</div>
      <ChatMessage
        v-for="(m, i) in chat.messages"
        :key="i"
        :message="m"
      />
    </div>
    <form @submit.prevent="onSubmit">
      <input
        v-model="input"
        placeholder="输入消息，Enter 发送…"
        autocomplete="off"
        :disabled="chat.streaming"
      />
      <button type="submit" :disabled="chat.streaming || !input.trim()">发送</button>
    </form>
  </section>
</template>

<style scoped>
section {
  flex: 1;
  display: flex;
  flex-direction: column;
  height: calc(100vh - 33px);
}
#messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.empty {
  color: var(--muted);
  text-align: center;
  margin-top: 40px;
}
form {
  display: flex;
  gap: 10px;
  padding: 16px 24px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}
form input {
  flex: 1;
}
</style>
