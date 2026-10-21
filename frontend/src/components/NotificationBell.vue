<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { api } from '../api'
import type { InboxNotification } from '../types'
import { beijingFromDb } from '../utils/datetime'
import { renderMarkdown } from '../utils/markdown'

const POLL_MS = 60_000
const LONG_CONTENT_CHARS = 120

const emit = defineEmits<{
  openLink: [link: string]
}>()

const open = ref(false)
const loading = ref(false)
const error = ref('')
const items = ref<InboxNotification[]>([])
const unreadCount = ref(0)
const unreadOnly = ref(false)
const expandedIds = ref<number[]>([])
const panel = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
let pollTimer: number | undefined

const badge = computed(() => (unreadCount.value > 99 ? '99+' : String(unreadCount.value)))
const triggerLabel = computed(() =>
  unreadCount.value ? `站内消息，${unreadCount.value} 条未读` : '站内消息'
)

function messageOf(exc: unknown, fallback: string) {
  const message = (exc as { message?: unknown } | null)?.message
  return typeof message === 'string' && message ? message : fallback
}

async function refreshCount() {
  try {
    unreadCount.value = (await api.notifications(true, 1)).unread_count
  } catch {
    // 角标刷新失败不打扰用户，下一轮再试
  }
}

async function loadList() {
  loading.value = true
  error.value = ''
  try {
    const data = await api.notifications(unreadOnly.value, 50)
    items.value = data.items
    unreadCount.value = data.unread_count
  } catch (exc) {
    error.value = messageOf(exc, '消息加载失败，请稍后再试')
  } finally {
    loading.value = false
  }
}

async function toggle() {
  open.value = !open.value
  if (!open.value) return
  await loadList()
  await nextTick(() => panel.value?.focus())
}

function close() {
  open.value = false
  trigger.value?.focus()
}

function isExpanded(id: number) {
  return expandedIds.value.includes(id)
}

function isLong(item: InboxNotification) {
  const content = item.content || ''
  return content.length > LONG_CONTENT_CHARS || content.split('\n').length > 3
}

async function markRead(item: InboxNotification) {
  if (item.is_read) return
  item.is_read = true
  unreadCount.value = Math.max(0, unreadCount.value - 1)
  try {
    await api.markNotificationRead(item.id)
  } catch (exc) {
    item.is_read = false
    unreadCount.value += 1
    error.value = messageOf(exc, '标记已读失败')
  }
}

async function toggleItem(item: InboxNotification) {
  expandedIds.value = isExpanded(item.id)
    ? expandedIds.value.filter((id) => id !== item.id)
    : [...expandedIds.value, item.id]
  await markRead(item)
}

async function openLink(item: InboxNotification) {
  await markRead(item)
  if (!item.link) return
  emit('openLink', item.link)
  close()
}

async function markAll() {
  try {
    await api.markAllNotificationsRead()
    items.value.forEach((item) => {
      item.is_read = true
    })
    unreadCount.value = 0
    if (unreadOnly.value) items.value = []
  } catch (exc) {
    error.value = messageOf(exc, '全部已读失败')
  }
}

async function remove(item: InboxNotification) {
  try {
    await api.deleteNotification(item.id)
    items.value = items.value.filter((candidate) => candidate.id !== item.id)
    if (!item.is_read) unreadCount.value = Math.max(0, unreadCount.value - 1)
  } catch (exc) {
    error.value = messageOf(exc, '删除失败')
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && open.value) close()
}

function onDocumentClick(event: MouseEvent) {
  if (!open.value) return
  const target = event.target as Node | null
  if (target && (panel.value?.contains(target) || trigger.value?.contains(target))) return
  open.value = false
}

onMounted(() => {
  void refreshCount()
  pollTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible') void refreshCount()
  }, POLL_MS)
  document.addEventListener('keydown', onKeydown)
  document.addEventListener('click', onDocumentClick)
})

onBeforeUnmount(() => {
  window.clearInterval(pollTimer)
  document.removeEventListener('keydown', onKeydown)
  document.removeEventListener('click', onDocumentClick)
})
</script>

<template>
  <div class="notification-bell">
    <button
      ref="trigger"
      type="button"
      class="bell-trigger"
      :aria-label="triggerLabel"
      :aria-expanded="open"
      aria-haspopup="dialog"
      data-testid="notification-bell"
      @click.stop="toggle"
    >
      <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
        <path
          d="M12 3a6 6 0 0 0-6 6v3.6L4.3 15.4A1 1 0 0 0 5.2 17h13.6a1 1 0 0 0 .9-1.6L18 12.6V9a6 6 0 0 0-6-6Zm0 18a2.5 2.5 0 0 0 2.4-2h-4.8A2.5 2.5 0 0 0 12 21Z"
          fill="currentColor"
        />
      </svg>
      消息
      <b v-if="unreadCount" class="bell-badge" data-testid="notification-unread">{{ badge }}</b>
    </button>

    <section
      v-if="open"
      ref="panel"
      class="bell-panel"
      role="dialog"
      aria-label="站内消息"
      tabindex="-1"
      data-testid="notification-panel"
    >
      <header class="bell-head">
        <strong>站内消息</strong>
        <label class="bell-filter">
          <input v-model="unreadOnly" type="checkbox" @change="loadList" />
          只看未读
        </label>
        <button type="button" :disabled="!unreadCount" @click="markAll">全部已读</button>
        <button type="button" class="bell-close" aria-label="关闭站内消息" @click="close">×</button>
      </header>

      <p v-if="error" class="bell-error" role="alert">{{ error }}</p>
      <p v-if="loading && !items.length" class="bell-empty">正在加载…</p>
      <p v-else-if="!items.length" class="bell-empty">{{ unreadOnly ? '没有未读消息' : '暂无消息' }}</p>
      <ol v-else class="bell-list">
        <li
          v-for="item in items"
          :key="item.id"
          :class="['bell-item', `is-${item.level}`, { 'is-unread': !item.is_read }]"
          :data-testid="`notification-${item.id}`"
        >
          <div class="bell-item-head">
            <i class="bell-dot" aria-hidden="true"></i>
            <strong>{{ item.title }}</strong>
            <time :datetime="item.created_at || undefined">{{ beijingFromDb(item.created_at) }}</time>
          </div>
          <div
            v-if="item.content"
            :class="['bell-content', { 'is-collapsed': isLong(item) && !isExpanded(item.id) }]"
            v-html="renderMarkdown(item.content)"
          ></div>
          <div class="bell-actions">
            <button v-if="isLong(item)" type="button" @click="toggleItem(item)">
              {{ isExpanded(item.id) ? '收起' : '展开全文' }}
            </button>
            <button v-if="item.link" type="button" @click="openLink(item)">去看看</button>
            <button v-if="!item.is_read" type="button" @click="markRead(item)">标为已读</button>
            <button type="button" class="bell-delete" @click="remove(item)">删除</button>
          </div>
        </li>
      </ol>
    </section>
  </div>
</template>

<style scoped>
.notification-bell {
  position: relative;
}

.bell-trigger {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.bell-badge {
  min-width: 18px;
  padding: 1px 5px;
  border-radius: 9px;
  background: var(--danger);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  line-height: 16px;
  text-align: center;
}

.bell-panel {
  position: fixed;
  top: 64px;
  right: 16px;
  z-index: 60;
  display: flex;
  flex-direction: column;
  width: min(420px, calc(100vw - 32px));
  max-height: min(72vh, 680px);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: 0 18px 40px rgba(0, 0, 0, 0.55);
  color: var(--text);
  outline: none;
}

.bell-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
}

.bell-head strong {
  flex: 1;
  color: var(--gold-2);
  font-size: 14px;
}

.bell-filter {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--muted);
  font-size: 12px;
}

.bell-close {
  padding: 2px 8px;
  font-size: 15px;
  line-height: 1;
}

.bell-error,
.bell-empty {
  margin: 0;
  padding: 18px 14px;
  color: var(--muted);
  font-size: 13px;
  text-align: center;
}

.bell-error {
  color: var(--danger);
}

.bell-list {
  margin: 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}

.bell-item {
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
}

.bell-item:last-child {
  border-bottom: 0;
}

.bell-item-head {
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: baseline;
}

.bell-item-head strong {
  overflow-wrap: anywhere;
  color: var(--muted);
  font-size: 13px;
  font-weight: 600;
}

.bell-item.is-unread .bell-item-head strong {
  color: var(--text);
}

.bell-item-head time {
  color: var(--muted-2);
  font-size: 11px;
  white-space: nowrap;
}

.bell-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--muted-2);
}

.bell-item.is-success .bell-dot {
  background: var(--ok);
}

.bell-item.is-warning .bell-dot {
  background: var(--warning);
}

.bell-item.is-error .bell-dot {
  background: var(--danger);
}

.bell-item.is-info .bell-dot {
  background: var(--cyan);
}

.bell-content {
  margin: 8px 0 0 16px;
  color: #d7d1be;
  font-size: 12.5px;
  line-height: 1.65;
  overflow-wrap: anywhere;
}

.bell-content.is-collapsed {
  max-height: 6.6em;
  overflow: hidden;
  mask-image: linear-gradient(to bottom, #000 60%, transparent);
}

.bell-content :deep(p),
.bell-content :deep(ol),
.bell-content :deep(ul) {
  margin: 0 0 6px;
}

.bell-content :deep(ol),
.bell-content :deep(ul) {
  padding-left: 18px;
}

.bell-content :deep(strong) {
  color: var(--gold-2);
}

.bell-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 8px 0 0 16px;
}

.bell-actions button {
  padding: 3px 8px;
  font-size: 11.5px;
}

.bell-actions .bell-delete {
  border-color: var(--line-strong);
  color: var(--muted);
}

@media (max-width: 720px) {
  .bell-panel {
    top: 12px;
    left: 16px;
    right: 16px;
    width: auto;
    max-height: calc(100vh - 24px);
  }
}
</style>
