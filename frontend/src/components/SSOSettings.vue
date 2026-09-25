<script setup lang="ts">
// V3-T8 SSO + 数据脱敏页签：provider 列表 / stub 演示登录 / PII 脱敏工具

import { onMounted, ref } from 'vue'
import * as api from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { PIIConfigResponse, SSOProviderItem } from '@/types'

const auth = useAuthStore()

const providers = ref<SSOProviderItem[]>([])
const loading = ref(false)
const error = ref('')

// stub 演示登录
const demoEmail = ref('')
const demoDisplay = ref('')
const demoLogging = ref(false)
const demoMsg = ref('')

// PII 脱敏工具
const piiConfig = ref<PIIConfigResponse | null>(null)
const piiText = ref('')
const piiMode = ref('partial')
const piiResult = ref<{ masked: string; matches: { type: string; value: string }[] } | null>(null)
const masking = ref(false)

const modeLabels: Record<string, string> = {
  partial: 'Partial（保留首尾）',
  mask: 'Mask（整段打码）',
  redact: 'Redact（替换占位）',
}

const typeLabels: Record<string, string> = {
  email: '邮箱',
  phone_cn: '手机号',
  id_card_cn: '身份证',
  ip: 'IP',
  credit_card: '银行卡',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [p, c] = await Promise.all([api.listSSOProviders(), api.getPIIConfig()])
    providers.value = p.items
    piiConfig.value = c
    piiMode.value = c.mode
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function onDemoLogin() {
  const email = demoEmail.value.trim()
  if (!email) {
    alert('请填写 SSO 登录邮箱')
    return
  }
  if (!auth.tenantId) {
    alert('未获取到当前租户信息，请重新登录')
    return
  }
  demoLogging.value = true
  demoMsg.value = ''
  try {
    const r = await api.ssoDemoLogin({
      email,
      tenant_id: auth.tenantId,
      display_name: demoDisplay.value.trim() || null,
    })
    // 演示：直接切换为 SSO 会话
    auth.tenantId = r.tenant_id
    auth.userId = r.user_id
    auth.email = r.email
    auth.token = r.access_token
    api.saveAuth(r)
    demoMsg.value = `SSO 登录成功（${r.email}，${r.bound ? '新建绑定' : '复用绑定'}）`
  } catch (e) {
    demoMsg.value = (e as Error).message
  } finally {
    demoLogging.value = false
  }
}

async function onMask() {
  if (!piiText.value.trim()) {
    alert('请粘贴要脱敏的文本')
    return
  }
  masking.value = true
  try {
    const r = await api.maskPII({ text: piiText.value, mode: piiMode.value })
    piiResult.value = {
      masked: r.masked,
      matches: r.matches.map((m) => ({ type: m.type, value: m.value })),
    }
  } catch (e) {
    alert((e as Error).message)
  } finally {
    masking.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="sso">
    <div class="toolbar">
      <div class="left">
        <h2>SSO 与数据脱敏</h2>
        <span class="count">{{ providers.length }} 个 SSO provider</span>
      </div>
      <div class="right">
        <button class="btn ghost" @click="load">刷新</button>
      </div>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="error" class="err">{{ error }}</div>

    <!-- SSO providers -->
    <div class="section-title">SSO Providers（OAuth2 授权码流）</div>
    <div v-if="providers.length === 0" class="empty small">未配置 SSO provider</div>
    <div v-for="p in providers" :key="p.name" class="card">
      <div class="card-head">
        <span class="badge" :class="{ stub: p.is_stub }">{{ p.is_stub ? 'stub' : 'sso' }}</span>
        <span class="name">{{ p.name }}</span>
        <span class="ver">{{ p.is_stub ? '离线演示 IdP' : '已配置' }}</span>
      </div>
    </div>

    <!-- stub 演示登录 -->
    <div class="section-title">Stub 演示登录（免跳转，走完整授权码流）</div>
    <div class="card">
      <div class="row">
        <input
          v-model="demoEmail"
          class="inp grow"
          placeholder="SSO 用户邮箱，如 alice@example.com"
        />
        <input
          v-model="demoDisplay"
          class="inp grow"
          placeholder="显示名（可选）"
        />
        <button class="btn primary" :disabled="demoLogging" @click="onDemoLogin">
          {{ demoLogging ? '登录中…' : 'SSO 登录' }}
        </button>
      </div>
      <div v-if="demoMsg" class="result" :class="{ err: demoMsg.startsWith('失败') }">
        {{ demoMsg }}
      </div>
      <div class="hint">
        模拟 IdP 登录：输入邮箱 → 后端 StubIdP 签发授权码 → 兑换 token →
        拉取用户信息 → 绑定/创建用户 → 签发 JWT（生产环境替换为真实 IdP 的
        authorize/callback 流程，见 ROADMAP V3-T8）。
      </div>
    </div>

    <!-- PII 脱敏工具 -->
    <div class="section-title">
      PII 脱敏工具
      <span v-if="piiConfig" class="ver">
        · 当前默认模式 {{ piiConfig.mode }}{{ piiConfig.types ? ' / ' + piiConfig.types.join(', ') : ' / 全部类型' }}
      </span>
    </div>
    <div class="card">
      <div class="row">
        <select v-model="piiMode" class="sel">
          <option v-for="(label, key) in modeLabels" :key="key" :value="key">{{ label }}</option>
        </select>
      </div>
      <textarea
        v-model="piiText"
        class="ta"
        placeholder="粘贴含 PII 的文本，如：联系 a@x.com 或手机 13812345678，身份证 110101199003074512"
      />
      <div class="actions">
        <button class="btn primary" :disabled="masking" @click="onMask">
          {{ masking ? '脱敏中…' : '检测并脱敏' }}
        </button>
      </div>
      <div v-if="piiResult" class="result">
        <div class="res-label">脱敏结果：</div>
        <div class="res-text">{{ piiResult.masked }}</div>
        <div class="res-meta">
          <span v-for="m in piiResult.matches" :key="m.type + m.value" class="cap">
            {{ typeLabels[m.type] || m.type }} → {{ m.value }}
          </span>
          <span v-if="piiResult.matches.length === 0" class="ver">未检测到 PII</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sso {
  padding: 16px 24px;
  overflow-y: auto;
  height: 100%;
  box-sizing: border-box;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.toolbar .left {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.toolbar h2 {
  font-size: 16px;
  margin: 0;
}
.toolbar .count {
  font-size: 12px;
  color: var(--muted);
}
.section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 20px 0 8px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 16px;
  margin-bottom: 10px;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-head .name {
  font-weight: 600;
  font-size: 14px;
  flex: 1;
}
.ver {
  font-size: 11px;
  color: var(--muted);
}
.badge {
  font-size: 10px;
  padding: 1px 8px;
  border-radius: 4px;
  color: #fff;
  background: #8b5cf6;
}
.badge.stub {
  background: #10b981;
}
.row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
  align-items: center;
}
.inp,
.sel,
.ta {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 8px;
  font-size: 13px;
  box-sizing: border-box;
}
.inp.grow {
  flex: 1;
}
.ta {
  width: 100%;
  min-height: 80px;
  resize: vertical;
  margin-bottom: 8px;
}
.btn {
  padding: 6px 12px;
  font-size: 12px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  cursor: pointer;
}
.btn.primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.actions {
  display: flex;
  gap: 8px;
}
.result {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  margin-top: 8px;
  font-size: 12px;
}
.result.err {
  color: #ff6b6b;
  border-color: #ff6b6b66;
}
.res-label {
  color: var(--muted);
  margin-bottom: 4px;
}
.res-text {
  white-space: pre-wrap;
  word-break: break-all;
}
.res-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.cap {
  font-size: 10px;
  padding: 1px 8px;
  border-radius: 4px;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--accent);
}
.hint {
  font-size: 11px;
  color: var(--muted);
  margin-top: 8px;
}
.empty {
  color: var(--muted);
  font-size: 13px;
  text-align: center;
  padding: 40px 0;
}
.empty.small {
  padding: 12px 0;
  font-size: 12px;
}
.err {
  color: #ff6b6b;
  font-size: 12px;
}
</style>
