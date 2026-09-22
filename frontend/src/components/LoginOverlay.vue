<script setup lang="ts">
import { ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const name = ref('')
const email = ref('')
const password = ref('')
const apiKey = ref<string | null>(null)

watch(
  () => auth.authMode,
  () => {
    apiKey.value = null
    auth.error = ''
  },
)

async function onSubmit() {
  auth.error = ''
  if (!email.value || !password.value) {
    auth.error = '邮箱和密码必填'
    return
  }
  if (auth.authMode === 'register') {
    if (!name.value) {
      auth.error = '租户名必填'
      return
    }
    const k = await auth.doRegister({
      name: name.value,
      email: email.value,
      password: password.value,
    })
    if (k) {
      apiKey.value = k
      password.value = ''
    }
  } else {
    await auth.doLogin({ email: email.value, password: password.value })
  }
}

function switchMode(mode: 'login' | 'register') {
  auth.authMode = mode
}
</script>

<template>
  <div class="overlay" v-if="!auth.token">
    <div class="card">
      <h2>AgentForge</h2>
      <div class="sub">多租户 · SSE · OpenSandbox</div>
      <div class="tab">
        <button
          :class="{ active: auth.authMode === 'login' }"
          @click="switchMode('login')"
        >
          登录
        </button>
        <button
          :class="{ active: auth.authMode === 'register' }"
          @click="switchMode('register')"
        >
          注册租户
        </button>
      </div>
      <form @submit.prevent="onSubmit">
        <input
          v-if="auth.authMode === 'register'"
          v-model="name"
          placeholder="租户名"
        />
        <input v-model="email" type="email" placeholder="邮箱" autocomplete="email" />
        <input
          v-model="password"
          type="password"
          placeholder="密码（至少 8 位）"
          autocomplete="current-password"
        />
        <div class="err">{{ auth.error }}</div>
        <button type="submit">{{ auth.authMode === 'login' ? '登录' : '注册' }}</button>
      </form>
      <div class="hint" v-if="apiKey">
        注册成功！请妥善保管 API Key（仅显示一次）：<br />
        <code>{{ apiKey }}</code>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.85);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
}
.card {
  width: 360px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 28px;
}
h2 {
  font-size: 18px;
  margin-bottom: 4px;
}
.sub {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 18px;
}
.tab {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}
.tab button {
  flex: 1;
  background: var(--border);
  padding: 8px;
  font-size: 13px;
}
.tab button.active {
  background: var(--accent);
}
form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.err {
  color: #ff6b6b;
  font-size: 12px;
  min-height: 16px;
}
.hint {
  font-size: 11px;
  color: var(--muted);
  margin-top: 12px;
  line-height: 1.6;
  word-break: break-all;
}
.hint code {
  color: var(--ok);
  word-break: break-all;
}
</style>
