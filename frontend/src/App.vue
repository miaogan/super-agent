<script setup lang="ts">
import { computed, ref } from 'vue'
import LoginOverlay from '@/components/LoginOverlay.vue'
import Sidebar from '@/components/Sidebar.vue'
import ChatPanel from '@/components/ChatPanel.vue'
import WorkflowBuilder from '@/components/workflow/WorkflowBuilder.vue'
import WorkflowRunner from '@/components/workflow/WorkflowRunner.vue'
import SubAgentManager from '@/components/SubAgentManager.vue'
import MarketPlace from '@/components/MarketPlace.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const loggedIn = computed(() => !!auth.token)
type View = 'chat' | 'workflow' | 'run' | 'agents' | 'market'
const view = ref<View>('chat')
</script>

<template>
  <LoginOverlay v-if="!loggedIn" />
  <div class="layout" v-else>
    <Sidebar :view="view" @switch-view="(v) => (view = v)" />
    <main class="main-wrap">
      <div class="topbar">
        <div class="view-tabs">
          <button
            class="tab"
            :class="{ on: view === 'chat' }"
            @click="view = 'chat'"
          >
            对话
          </button>
          <button
            class="tab"
            :class="{ on: view === 'workflow' }"
            @click="view = 'workflow'"
          >
            Workflow 画布
          </button>
          <button
            class="tab"
            :class="{ on: view === 'run' }"
            @click="view = 'run'"
          >
            Workflow 运行
          </button>
          <button
            class="tab"
            :class="{ on: view === 'agents' }"
            @click="view = 'agents'"
          >
            子代理
          </button>
          <button
            class="tab"
            :class="{ on: view === 'market' }"
            @click="view = 'market'"
          >
            模板市场
          </button>
        </div>
        <div class="right">
          <span class="me">{{ auth.email }}</span>
          <button class="ghost small" @click="auth.logout()">退出</button>
        </div>
      </div>
      <div class="content">
        <ChatPanel v-if="view === 'chat'" />
        <WorkflowBuilder v-else-if="view === 'workflow'" />
        <WorkflowRunner v-else-if="view === 'run'" />
        <SubAgentManager v-else-if="view === 'agents'" />
        <MarketPlace v-else-if="view === 'market'" />
      </div>
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  height: 100vh;
}
.main-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.topbar {
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: var(--muted);
  background: var(--panel);
  height: 44px;
  flex-shrink: 0;
}
.view-tabs {
  display: flex;
  gap: 4px;
}
.tab {
  padding: 6px 16px;
  font-size: 12px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--muted);
  cursor: pointer;
}
.tab.on {
  color: var(--text);
  border-bottom-color: var(--accent);
}
.tab:hover {
  color: var(--text);
}
.right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.topbar .me {
  color: var(--text);
}
.small {
  padding: 4px 10px;
  font-size: 11px;
}
.content {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
</style>
