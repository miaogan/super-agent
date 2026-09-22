<script setup lang="ts">
import type { RagCitation, RagContext } from '@/types'

defineProps<{
  message: {
    role: 'user' | 'ai' | 'event' | 'citation'
    content: string
    subtype?: '' | 'subagent' | 'memory'
    citations?: RagCitation[]
    contexts?: RagContext[]
    ragMode?: string
    ragError?: string | null
  }
}>()

function citLabel(c: RagCitation, i: number): string {
  const parts: string[] = []
  if (c.title) parts.push(c.title)
  if (c.page != null) parts.push(`p.${c.page}`)
  if (!parts.length && c.doc_id) parts.push(c.doc_id)
  if (!parts.length) parts.push(`#${i + 1}`)
  return parts.join(' · ')
}

function ctxPreview(ctx: RagContext, n = 120): string {
  const t = ctx.content || ''
  return t.length <= n ? t : t.slice(0, n) + '…'
}

function modeBadge(mode?: string): string {
  if (mode === 'live') return '🌐 实时检索'
  if (mode === 'degraded') return '⚠️ 降级'
  if (mode === 'stub') return '🧪 stub'
  return mode || ''
}
</script>

<template>
  <div v-if="message.role === 'user'" class="msg user">{{ message.content }}</div>
  <div v-else-if="message.role === 'ai'" class="msg ai">{{ message.content }}<span v-if="!message.content" class="cursor">▍</span></div>
  <div v-else-if="message.role === 'citation'" class="citation">
    <div class="cit-header">
      <span class="cit-icon">📚</span>
      <span class="cit-title">知识检索引用</span>
      <span class="cit-mode">{{ modeBadge(message.ragMode) }}</span>
      <span v-if="message.ragError" class="cit-error">⚠ {{ message.ragError }}</span>
    </div>
    <div v-if="message.contexts && message.contexts.length" class="cit-contexts">
      <div v-for="(ctx, i) in message.contexts" :key="i" class="cit-ctx">
        <div class="cit-ctx-text">{{ ctxPreview(ctx) }}</div>
        <div class="cit-ctx-meta">
          <span v-if="ctx.source" class="cit-source">{{ ctx.source }}</span>
          <span v-if="ctx.score" class="cit-score">score {{ ctx.score.toFixed(2) }}</span>
        </div>
      </div>
    </div>
    <div v-if="message.citations && message.citations.length" class="cit-list">
      <div v-for="(c, i) in message.citations" :key="i" class="cit-item">
        <span class="cit-no">[{{ i + 1 }}]</span>
        <a v-if="c.url" :href="c.url" target="_blank" rel="noopener" class="cit-link">{{ citLabel(c, i) }}</a>
        <span v-else class="cit-text">{{ citLabel(c, i) }}</span>
      </div>
    </div>
    <div v-else-if="!message.contexts?.length" class="cit-empty">无召回内容</div>
  </div>
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
.citation {
  align-self: flex-start;
  max-width: 88%;
  background: var(--panel);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
}
.cit-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--muted);
}
.cit-icon {
  font-size: 14px;
}
.cit-title {
  font-weight: 600;
  color: var(--text);
}
.cit-mode {
  margin-left: auto;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--ai);
  font-size: 11px;
}
.cit-error {
  color: #c0392b;
}
.cit-contexts {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 8px;
}
.cit-ctx {
  background: var(--ai);
  border-radius: 6px;
  padding: 6px 8px;
}
.cit-ctx-text {
  font-size: 12px;
  line-height: 1.5;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
.cit-ctx-meta {
  display: flex;
  gap: 10px;
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
}
.cit-list {
  display: flex;
  flex-direction: column;
  gap: 3px;
  border-top: 1px dashed var(--border);
  padding-top: 6px;
}
.cit-item {
  font-size: 12px;
  color: var(--muted);
}
.cit-no {
  color: var(--accent);
  font-weight: 600;
  margin-right: 4px;
}
.cit-link {
  color: var(--accent);
  text-decoration: underline;
}
.cit-text {
  color: var(--text);
}
.cit-empty {
  font-size: 12px;
  color: var(--muted);
  font-style: italic;
}
</style>
