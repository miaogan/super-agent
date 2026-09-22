<script setup lang="ts">
import { computed } from 'vue'
import LoginOverlay from '@/components/LoginOverlay.vue'
import Sidebar from '@/components/Sidebar.vue'
import ChatPanel from '@/components/ChatPanel.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const loggedIn = computed(() => !!auth.token)
</script>

<template>
  <LoginOverlay v-if="!loggedIn" />
  <div class="layout" v-else>
    <Sidebar />
    <main class="chat-wrap">
      <div class="topbar">
        <span class="me">{{ auth.email }}</span>
        <button class="ghost small" @click="auth.logout()">退出</button>
      </div>
      <ChatPanel />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  height: 100vh;
}
.chat-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
}
.topbar {
  padding: 8px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  align-items: center;
  font-size: 12px;
  color: var(--muted);
  background: var(--panel);
}
.topbar .me {
  color: var(--text);
}
.small {
  padding: 4px 10px;
  font-size: 11px;
}
</style>
