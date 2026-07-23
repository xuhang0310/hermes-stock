<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api'
import type { AgentSkill } from '../types'
import { renderMarkdown } from '../utils/markdown'

const skills = ref<AgentSkill[]>([])
const selected = ref<AgentSkill | null>(null)
const loading = ref(true)
const detailLoading = ref(false)
const error = ref('')
const query = ref('')
const activeCategory = ref('all')

const categoryLabels: Record<string, string> = {
  all: '全部',
  workflow: '研究流程',
  data: '数据治理',
  market: '市场行情',
  fundamental: '基本面',
  intelligence: '事件情报',
  quantitative: '量化分析',
  research: '深度研究',
  portfolio: '账户研究',
  risk: '风险控制',
  knowledge: '知识引用',
  artifact: '文件产物',
  execution: '代码执行'
}

const roleLabels: Record<string, string> = {
  'stock-pm': '总调度',
  'research-analyst': '个股研究员',
  'quant-analyst': '量化分析师',
  'short-term-hunter': '短线猎手',
  'intelligence-officer': '情报官',
  'risk-officer': '风控复盘官',
  'position-entry-agent': '持仓录入 Agent',
  'watchlist-entry-agent': '自选录入 Agent'
}

const categories = computed(() => {
  const counts = new Map<string, number>()
  for (const skill of skills.value) counts.set(skill.category, (counts.get(skill.category) || 0) + 1)
  return [
    { key: 'all', label: '全部', count: skills.value.length },
    ...Array.from(counts, ([key, count]) => ({
      key,
      label: categoryLabels[key] || key,
      count
    }))
  ]
})

const filteredSkills = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return skills.value.filter((skill) => {
    if (activeCategory.value !== 'all' && skill.category !== activeCategory.value) return false
    if (!needle) return true
    return [
      skill.name,
      skill.display_name,
      skill.description,
      ...skill.tool_names,
      ...skill.roles
    ].some((value) => value.toLowerCase().includes(needle))
  })
})

const stats = computed(() => ({
  total: skills.value.length,
  ready: skills.value.filter((item) => item.availability === 'ready').length,
  tools: new Set(skills.value.flatMap((item) => item.tool_names)).size,
  resources: skills.value.reduce((sum, item) => sum + item.resources.length, 0)
}))

function riskText(skill: AgentSkill) {
  return skill.risk_level === 'high' ? '高风险' : skill.risk_level === 'medium' ? '需谨慎' : '低风险'
}

function roleText(role: string) {
  return roleLabels[role] || role
}

async function loadSkills() {
  loading.value = true
  error.value = ''
  try {
    skills.value = await api.agentSkills()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载技能失败'
  } finally {
    loading.value = false
  }
}

async function openSkill(skill: AgentSkill) {
  selected.value = skill
  detailLoading.value = true
  document.body.classList.add('skill-dialog-open')
  try {
    selected.value = { ...skill, ...(await api.agentSkill(skill.name)) }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载技能详情失败'
  } finally {
    detailLoading.value = false
  }
}

function closeSkill() {
  selected.value = null
  document.body.classList.remove('skill-dialog-open')
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && selected.value) closeSkill()
}

onMounted(() => {
  loadSkills()
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  document.body.classList.remove('skill-dialog-open')
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <section class="skill-square">
    <header class="skill-square-hero">
      <div>
        <span class="skill-square-eyebrow">AGENT SKILLS</span>
        <h1>技能广场</h1>
        <p>浏览系统当前可用的专业工作流。技能按 Agent Skills 规范组织，并在任务需要时渐进加载。</p>
      </div>
      <button class="skill-refresh" type="button" :disabled="loading" title="刷新技能" @click="loadSkills">
        <span aria-hidden="true">↻</span>
        {{ loading ? '刷新中' : '刷新' }}
      </button>
    </header>

    <section class="skill-square-stats" aria-label="技能概览">
      <div><strong>{{ stats.total }}</strong><span>标准技能</span></div>
      <div><strong>{{ stats.ready }}</strong><span>完整可用</span></div>
      <div><strong>{{ stats.tools }}</strong><span>声明工具</span></div>
      <div><strong>{{ stats.resources }}</strong><span>按需资料</span></div>
    </section>

    <section class="skill-square-toolbar">
      <label class="skill-search">
        <span aria-hidden="true">⌕</span>
        <input v-model="query" type="search" placeholder="搜索技能、用途、角色或工具" />
      </label>
      <div class="skill-categories" role="tablist" aria-label="技能分类">
        <button
          v-for="category in categories"
          :key="category.key"
          type="button"
          :class="{ active: activeCategory === category.key }"
          @click="activeCategory = category.key"
        >
          {{ category.label }} <span>{{ category.count }}</span>
        </button>
      </div>
    </section>

    <div v-if="error" class="notice notice-danger">{{ error }}</div>
    <div v-if="loading" class="skill-square-loading">正在读取技能目录...</div>
    <div v-else-if="!filteredSkills.length" class="skill-square-empty">
      <strong>没有匹配的技能</strong>
      <span>调整关键词或分类后再试。</span>
    </div>

    <section v-else class="skill-market-grid">
      <article v-for="skill in filteredSkills" :key="skill.name" class="skill-market-card">
        <header>
          <div class="skill-market-mark">{{ skill.display_name.slice(0, 1) }}</div>
          <div class="skill-market-heading">
            <span>{{ categoryLabels[skill.category] || skill.category }}</span>
            <h2>{{ skill.display_name }}</h2>
            <code>{{ skill.name }}</code>
          </div>
          <span class="skill-availability" :class="skill.availability">
            {{ skill.availability === 'ready' ? '可用' : '部分可用' }}
          </span>
        </header>

        <p>{{ skill.description }}</p>

        <div class="skill-market-meta">
          <span>v{{ skill.version }}</span>
          <span :class="`risk-${skill.risk_level}`">{{ riskText(skill) }}</span>
          <span>{{ skill.tool_names.length }} 个工具</span>
          <span>{{ skill.criteria.length }} 项验收</span>
        </div>

        <footer>
          <div class="skill-role-preview">
            <span v-if="!skill.roles.length">全部数字人</span>
            <span v-for="role in skill.roles.slice(0, 2)" :key="role">{{ roleText(role) }}</span>
            <span v-if="skill.roles.length > 2">+{{ skill.roles.length - 2 }}</span>
          </div>
          <button type="button" @click="openSkill(skill)">
            查看详情 <span aria-hidden="true">→</span>
          </button>
        </footer>
      </article>
    </section>

    <div v-if="selected" class="skill-dialog-backdrop" @click.self="closeSkill">
      <article class="skill-dialog" role="dialog" aria-modal="true" :aria-label="`${selected.display_name}详情`">
        <header class="skill-dialog-header">
          <div class="skill-market-mark large">{{ selected.display_name.slice(0, 1) }}</div>
          <div>
            <span>{{ categoryLabels[selected.category] || selected.category }} · v{{ selected.version }}</span>
            <h2>{{ selected.display_name }}</h2>
            <code>{{ selected.name }}</code>
          </div>
          <button type="button" title="关闭" aria-label="关闭" @click="closeSkill">×</button>
        </header>

        <div v-if="detailLoading" class="skill-square-loading">正在加载 SKILL.md...</div>
        <div v-else class="skill-dialog-body">
          <section class="skill-detail-summary">
            <p>{{ selected.description }}</p>
            <div>
              <span class="skill-availability" :class="selected.availability">
                {{ selected.availability === 'ready' ? '运行环境完整' : `可用度 ${selected.readiness_percent}%` }}
              </span>
              <span>{{ riskText(selected) }}</span>
              <span>SHA {{ selected.content_sha256.slice(0, 10) }}</span>
            </div>
          </section>

          <section class="skill-detail-columns">
            <div>
              <h3>适用数字人</h3>
              <p v-if="!selected.roles.length">所有具备 Agent 使用权限的数字人</p>
              <div v-else class="skill-detail-tags">
                <span v-for="role in selected.roles" :key="role">{{ roleText(role) }}</span>
              </div>
            </div>
            <div>
              <h3>输出合同</h3>
              <div class="skill-detail-tags">
                <span v-for="item in selected.output_contract" :key="item">{{ item }}</span>
              </div>
            </div>
          </section>

          <section>
            <h3>验收标准</h3>
            <div v-if="selected.criteria.length" class="skill-criteria-list">
              <div v-for="criterion in selected.criteria" :key="criterion.code">
                <code>{{ criterion.code }}</code>
                <strong>{{ criterion.description }}</strong>
                <span>{{ criterion.required ? '必选' : '可选' }} · 至少 {{ criterion.min_evidence }} 条已验证证据</span>
              </div>
            </div>
            <p v-else class="skill-detail-muted">流程型技能不单独增加证据门槛。</p>
          </section>

          <section>
            <h3>工具与资源</h3>
            <div class="skill-tools">
              <span
                v-for="tool in selected.tool_names"
                :key="tool"
                :class="{ missing: selected.unavailable_tool_names?.includes(tool) }"
              >{{ tool }}</span>
            </div>
            <p v-if="selected.unavailable_tool_names?.length" class="skill-tool-warning">
              当前未启用：{{ selected.unavailable_tool_names.join('、') }}
            </p>
            <p v-if="selected.resources.length" class="skill-detail-muted">
              按需资料：{{ selected.resources.join('、') }}
            </p>
          </section>

          <section>
            <h3>专业工作流</h3>
            <div class="md-body skill-instructions" v-html="renderMarkdown(selected.instructions || '')"></div>
          </section>
        </div>
      </article>
    </div>
  </section>
</template>
