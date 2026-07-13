<script setup lang="ts">
import type { HealthPayload, UserPayload } from '../types'

import { computed } from 'vue'

export type PageKey =
  | 'workbench'
  | 'askStock'
  | 'positions'
  | 'positionChart'
  | 'watchlist'
  | 'digitalHumans'
  | 'rhythm'
  | 'knowledge'
  | 'settings'

const props = defineProps<{
  activePage: PageKey
  pageTitle: string
  health: HealthPayload | null
  user: UserPayload
}>()

defineEmits<{
  navigate: [page: PageKey]
  refresh: []
  logout: []
}>()

const canViewRhythm = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('system:view')
)
const canViewKnowledge = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('system:view')
)

const navItems = computed<Array<{ key: PageKey; label: string }>>(() => [
  { key: 'askStock', label: '问股' },
  { key: 'workbench', label: '工作台' },
  { key: 'digitalHumans', label: '数字人中心' },
  { key: 'positions', label: '持仓' },
  { key: 'watchlist', label: '自选池' },
  ...(canViewRhythm.value ? [{ key: 'rhythm' as PageKey, label: '定时任务' }] : []),
  ...(canViewKnowledge.value ? [{ key: 'knowledge' as PageKey, label: '知识库' }] : []),
  { key: 'settings', label: '设置' }
])
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
        <button
          v-for="item in navItems"
          :key="item.key"
          :class="{ active: activePage === item.key || (activePage === 'positionChart' && item.key === 'positions') }"
          type="button"
          @click="$emit('navigate', item.key)"
        >
          <img class="nav-icon" :src="`/icons/${item.key}.png`" alt="" aria-hidden="true" />
          <span>{{ item.label }}</span>
        </button>
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
          <p>问股、数字人、持仓、自选池和模型配置统一入口</p>
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
