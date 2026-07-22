<script setup lang="ts">
import type { HealthPayload, UserPayload } from '../types'

import { computed, ref } from 'vue'

export type PageKey =
  | 'workbench'
  | 'askStock'
  | 'positions'
  | 'positionChart'
  | 'watchlist'
  | 'digitalHumans'
  | 'rhythm'
  | 'knowledge'
  | 'knowledgeSearch'
  | 'workspaceDocs'
  | 'settings'

const props = defineProps<{
  activePage: PageKey
  pageTitle: string
  pageSubtitle?: string
  health: HealthPayload | null
  user: UserPayload
}>()

const emit = defineEmits<{
  navigate: [page: PageKey]
  refresh: []
  logout: []
}>()

const expandedMenu = ref<PageKey | null>(null)

const canViewRhythm = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('system:view')
)
const canViewKnowledge = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('system:view')
)

interface NavItem {
  key: PageKey
  label: string
  children?: NavItem[]
}

const navItems = computed<NavItem[]>(() => [
  { key: 'askStock', label: '问股' },
  { key: 'workbench', label: '工作台' },
  { key: 'digitalHumans', label: '数字人中心' },
  { key: 'positions', label: '持仓' },
  { key: 'watchlist', label: '自选池' },
  ...(canViewRhythm.value ? [{ key: 'rhythm' as PageKey, label: '定时任务' }] : []),
  ...(canViewKnowledge.value ? [{
    key: 'knowledge' as PageKey,
    label: '知识库',
    children: [
      { key: 'knowledge' as PageKey, label: '知识库' },
      { key: 'knowledgeSearch' as PageKey, label: '检索agent' },
      { key: 'workspaceDocs' as PageKey, label: '生成文档' }
    ]
  }] : []),
  { key: 'settings', label: '设置' }
])

function isExpanded(item: NavItem): boolean {
  if (!item.children) return false
  if (expandedMenu.value === item.key) return true
  if (item.children.some(c => props.activePage === c.key)) return true
  return false
}

function isParentActive(item: NavItem): boolean {
  if (item.children) {
    return item.children.some(child => props.activePage === child.key || (props.activePage === 'positionChart' && child.key === 'positions'))
  }
  return props.activePage === item.key || (props.activePage === 'positionChart' && item.key === 'positions')
}

function handleParentClick(item: NavItem) {
  if (item.children) {
    expandedMenu.value = expandedMenu.value === item.key ? null : item.key
    emit('navigate', item.children[0].key)
  } else {
    expandedMenu.value = null
    emit('navigate', item.key)
  }
}

function handleChildClick(key: PageKey) {
  emit('navigate', key)
}
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-logo-frame">
          <img src="/six-rabbits-logo.png" alt="Six Rabbits" />
        </div>
        <div>
          <strong>SixRabbits Stock</strong>
          <small>A股多智能体研究台</small>
        </div>
      </div>

      <nav class="nav-list">
        <template v-for="item in navItems" :key="item.key">
          <!-- parent item -->
          <button
            :class="{ active: isParentActive(item), expanded: isExpanded(item) }"
            type="button"
            @click="handleParentClick(item)"
          >
            <img class="nav-icon" :src="`/icons/${item.children ? 'knowledge' : item.key}.png`" alt="" aria-hidden="true" @error="(e) => ((e.target as HTMLImageElement).style.display = 'none')" />
            <span>{{ item.label }}</span>
            <svg v-if="item.children" class="nav-chevron" :class="{ open: isExpanded(item) }" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
              <path d="M4 2.5L7.5 6L4 9.5" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
          <!-- sub items -->
          <template v-if="item.children && isExpanded(item)">
            <button
              v-for="child in item.children"
              :key="child.key"
              :class="{ active: activePage === child.key }"
              class="nav-sub"
              type="button"
              @click="handleChildClick(child.key)"
            >
              <span>{{ child.label }}</span>
            </button>
          </template>
        </template>
      </nav>

      <div class="sidebar-footer">
        <span class="status-dot" :class="health?.ok ? 'ok' : 'danger'"></span>
        <span>{{ health?.ok ? 'MySQL 已连接' : '数据库待检查' }}</span>
      </div>
    </aside>

    <div class="main-frame">
      <header class="topbar">
        <div>
          <h1>{{ pageTitle }}</h1>
          <p>{{ pageSubtitle || '问股、数字人、持仓、自选池和模型配置统一入口' }}</p>
        </div>
        <div class="topbar-status">
          <span>{{ user.display_name }}</span>
          <span>{{ user.role }}</span>
          <span>{{ health?.journal_mode ?? 'mysql' }}</span>
          <button type="button" @click="$emit('refresh')">刷新</button>
          <button type="button" @click="$emit('logout')">退出</button>
        </div>
      </header>

      <main class="content">
        <slot />
      </main>
    </div>
  </div>
</template>
