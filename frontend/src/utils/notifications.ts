import type { PageKey } from '../components/AppShell.vue'

// 站内信的 link 由后端写入（如 /automatic-selection），这里换成前端页面键；不认识的链接不跳转。
const LINK_PAGES: Record<string, PageKey> = {
  // 量化选股并进了“选股”页，旧链接也跳到这里
  '/quant-selection': 'automaticSelection',
  '/automatic-selection': 'automaticSelection',
  '/industries': 'industryAnalysis',
}

export function pageForNotificationLink(link: string | null | undefined): PageKey | null {
  if (!link) return null
  const path = link.trim().replace(/\/+$/, '').toLowerCase()
  return LINK_PAGES[path] ?? null
}
