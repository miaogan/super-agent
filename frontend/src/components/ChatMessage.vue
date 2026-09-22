<script setup lang="ts">
defineProps<{
  message: {
    role: 'user' | 'ai' | 'event'
    content: string
    subtype?: '' | 'subagent' | 'memory'
  }
}>()
</script>

<template>
  <div v-if="message.role === 'user'" class="msg user">{{ message.content }}</div>
  <div v-else-if="message.role === 'ai'" class="msg ai">{{ message.content }}<span v-if="!message.content" class="cursor">▍</span></div>
  <div v-else class="event" :class="message.subtype" v-html="message.content"></div>
</template>

<style scoped>
.msg {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg.user {
  align-self: flex-end;
  background: var(--user);
  border-bottom-right-radius: 4px;
}
.msg.ai {
  align-self: flex-start;
  background: var(--ai);
  border-bottom-left-radius: 4px;
}
.cursor {
  animation: blink 1s infinite;
  color: var(--accent);
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.event {
  align-self: flex-start;
  font-size: 12px;
  color: var(--muted);
  background: transparent;
  border: 1px dashed var(--border);
  border-radius: 8px;
  padding: 6px 10px;
  max-width: 90%;
}
</style>
