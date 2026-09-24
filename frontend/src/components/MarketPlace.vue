<script setup lang="ts">
// V3-T6 模板市场页签：浏览 / 发布 / 安装 / 评分

import { onMounted, ref, computed } from 'vue'
import * as api from '@/api'
import type { MarketTemplateItem, MarketTemplateType } from '@/types'

const items = ref<MarketTemplateItem[]>([])
const loading = ref(false)
const error = ref('')
const typeFilter = ref('') // '' | workflow | skill | prompt

// 发布表单
const showPublish = ref(false)
const pubType = ref<MarketTemplateType>('workflow')
const pubSourceId = ref('')
const pubName = ref('')
const pubDesc = ref('')
const publishing = ref(false)

// 安装 / 评分状态
const installingId = ref('')
const ratingId = ref('')
const ratingScore = ref(5)

const typeLabels: Record<string, string> = {
  workflow: 'Workflow',
  skill: 'Skill',
  prompt: 'Prompt',
}

const filtered = computed(() => {
  if (!typeFilter.value) return items.value
  return items.value.filter((t) => t.type === typeFilter.value)
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const d = await api.listMarketTemplates(typeFilter.value || undefined)
    items.value = d.items
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

function onTypeFilterChange() {
  load()
}

function resetPublish() {
  pubType.value = 'workflow'
  pubSourceId.value = ''
  pubName.value = ''
  pubDesc.value = ''
}

async function onPublish() {
  if (!pubSourceId.value.trim()) {
    alert('请填写源资源 ID（workflow id / skill name / prompt key）')
    return
  }
  publishing.value = true
  try {
    await api.publishMarketTemplate({
      type: pubType.value,
      source_id: pubSourceId.value.trim(),
      name: pubName.value.trim() || undefined,
      description: pubDesc.value,
    })
    showPublish.value = false
    resetPublish()
    await load()
    alert('发布成功')
  } catch (e) {
    alert((e as Error).message)
  } finally {
    publishing.value = false
  }
}

async function onInstall(t: MarketTemplateItem) {
  installingId.value = t.id
  try {
    const r = await api.installMarketTemplate(t.id)
    await load()
    alert(`已安装为「${r.installed_name}」`)
  } catch (e) {
    alert((e as Error).message)
  } finally {
    installingId.value = ''
  }
}

async function onRate(t: MarketTemplateItem) {
  ratingId.value = t.id
  try {
    await api.rateMarketTemplate(t.id, ratingScore.value)
    await load()
    alert('评分成功')
  } catch (e) {
    alert((e as Error).message)
  } finally {
    ratingId.value = ''
  }
}

function stars(rating: number): string {
  const full = Math.round(rating)
  return '★'.repeat(full) + '☆'.repeat(5 - full)
}

onMounted(load)
</script>

<template>
  <div class="market">
    <div class="toolbar">
      <div class="left">
        <h2>模板市场</h2>
        <span class="count">{{ items.length }} 个模板</span>
      </div>
      <div class="right">
        <select v-model="typeFilter" class="sel" @change="onTypeFilterChange">
          <option value="">全部类型</option>
          <option value="workflow">Workflow</option>
          <option value="skill">Skill</option>
          <option value="prompt">Prompt</option>
        </select>
        <button class="btn ghost" @click="load">刷新</button>
        <button class="btn primary" @click="showPublish = !showPublish">
          {{ showPublish ? '收起发布' : '+ 发布模板' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="error" class="err">{{ error }}</div>

    <!-- 发布表单 -->
    <div v-if="showPublish" class="publish-card">
      <h3>发布模板（把自有资源上架）</h3>
      <div class="field">
        <label>类型</label>
        <select v-model="pubType" class="sel wide">
          <option value="workflow">Workflow（源 = workflow id）</option>
          <option value="skill">Skill（源 = skill 名称）</option>
          <option value="prompt">Prompt（源 = prompt key）</option>
        </select>
      </div>
      <div class="field">
        <label>源资源 ID</label>
        <input v-model="pubSourceId" class="inp" placeholder="workflow id / skill name / prompt key" />
      </div>
      <div class="field">
        <label>上架名称（留空 = 源资源名）</label>
        <input v-model="pubName" class="inp" placeholder="可自定义展示名" />
      </div>
      <div class="field">
        <label>描述</label>
        <textarea v-model="pubDesc" class="ta" placeholder="一句话介绍用途" />
      </div>
      <div class="actions">
        <button class="btn primary" :disabled="publishing" @click="onPublish">
          {{ publishing ? '发布中…' : '发布' }}
        </button>
        <button class="btn ghost" @click="showPublish = false">取消</button>
      </div>
    </div>

    <!-- 模板列表 -->
    <div v-if="!loading && !error" class="list">
      <div v-if="filtered.length === 0" class="empty">暂无模板，点击右上角发布第一个</div>
      <div v-for="t in filtered" :key="t.id" class="card">
        <div class="card-head">
          <span class="badge" :class="t.type">{{ typeLabels[t.type] }}</span>
          <span class="name">{{ t.name }}</span>
          <span class="rating">{{ stars(t.rating) }} {{ t.rating.toFixed(1) }}</span>
        </div>
        <div class="desc">{{ t.description || '（无描述）' }}</div>
        <div class="meta">
          <span>安装 {{ t.install_count }}</span>
          <span>· {{ t.rating_count }} 人评分</span>
          <span>· 发布者 {{ t.created_by }}</span>
        </div>
        <div class="card-actions">
          <button class="btn small primary" :disabled="installingId === t.id" @click="onInstall(t)">
            {{ installingId === t.id ? '安装中…' : '一键安装' }}
          </button>
          <select v-model="ratingScore" class="sel small">
            <option :value="5">5★</option>
            <option :value="4">4★</option>
            <option :value="3">3★</option>
            <option :value="2">2★</option>
            <option :value="1">1★</option>
          </select>
          <button class="btn ghost small" :disabled="ratingId === t.id" @click="onRate(t)">
            {{ ratingId === t.id ? '评分中…' : '评分' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.market {
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
.toolbar .right {
  display: flex;
  gap: 8px;
  align-items: center;
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
.btn.ghost {
  background: transparent;
}
.btn.small {
  padding: 4px 8px;
  font-size: 11px;
}
.sel {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
}
.sel.wide {
  width: 100%;
}
.sel.small {
  padding: 3px 6px;
  font-size: 11px;
}
.publish-card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 20px;
}
.publish-card h3 {
  font-size: 14px;
  margin: 0 0 12px;
}
.field {
  margin-bottom: 12px;
}
.field label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 4px;
}
.inp,
.ta {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 8px;
  font-size: 13px;
  box-sizing: border-box;
}
.ta {
  min-height: 60px;
  resize: vertical;
}
.actions {
  display: flex;
  gap: 8px;
}
.empty {
  color: var(--muted);
  font-size: 13px;
  text-align: center;
  padding: 40px 0;
}
.err {
  color: #ff6b6b;
  font-size: 12px;
}
.list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 16px;
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
.rating {
  color: #f5b301;
  font-size: 12px;
}
.badge {
  font-size: 10px;
  padding: 1px 8px;
  border-radius: 4px;
  color: #fff;
}
.badge.workflow {
  background: #10b981;
}
.badge.skill {
  background: #8b5cf6;
}
.badge.prompt {
  background: #f59e0b;
}
.desc {
  font-size: 12px;
  color: var(--muted);
  margin-top: 6px;
}
.meta {
  font-size: 11px;
  color: var(--muted);
  margin-top: 4px;
}
.card-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 10px;
}
</style>
