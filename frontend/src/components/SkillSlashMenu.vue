<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import type { AgentSkill } from '../types'

const props = withDefaults(defineProps<{
  chatMode: 'research' | 'stock' | 'knowledge'
  roleCode?: string | null
  selectedSkills?: AgentSkill[]
  disabled?: boolean
}>(), {
  roleCode: null,
  selectedSkills: () => [],
  disabled: false
})

const emit = defineEmits<{
  select: [payload: { skill: AgentSkill; start: number; end: number }]
  remove: [name: string]
}>()

const skills = ref<AgentSkill[]>([])
const loading = ref(false)
const loadError = ref('')
const open = ref(false)
const query = ref('')
const activeIndex = ref(0)
const triggerStart = ref(0)
const triggerEnd = ref(0)

const selectedNames = computed(() => new Set(props.selectedSkills.map((item) => item.name)))

const compatibleSkills = computed(() => skills.value.filter((item) => {
  if (!item.chat_modes.includes(props.chatMode)) return false
  return !item.roles.length || Boolean(props.roleCode && item.roles.includes(props.roleCode))
}))

const filteredSkills = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  const rows = compatibleSkills.value.filter((item) => {
    if (!keyword) return true
    return [item.display_name, item.name, item.summary, item.category]
      .some((value) => String(value || '').toLowerCase().includes(keyword))
  })
  return rows.sort((left, right) => {
    const selectedDelta = Number(selectedNames.value.has(right.name)) - Number(selectedNames.value.has(left.name))
    if (selectedDelta) return selectedDelta
    const readinessDelta = Number(right.availability === 'ready') - Number(left.availability === 'ready')
    if (readinessDelta) return readinessDelta
    return left.display_name.localeCompare(right.display_name, 'zh-CN')
  })
})

watch(filteredSkills, (rows) => {
  activeIndex.value = Math.min(activeIndex.value, Math.max(0, rows.length - 1))
})

watch(() => [props.chatMode, props.roleCode], () => {
  activeIndex.value = 0
})

async function loadSkills() {
  loading.value = true
  loadError.value = ''
  try {
    skills.value = await api.agentSkills()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '技能加载失败'
  } finally {
    loading.value = false
  }
}

function sync(text: string, caret: number) {
  if (props.disabled) {
    close()
    return
  }
  const prefix = text.slice(0, Math.max(0, caret))
  const match = prefix.match(/(^|\s)\/([^\s/]*)$/)
  if (!match) {
    close()
    return
  }
  query.value = match[2] || ''
  triggerStart.value = caret - query.value.length - 1
  triggerEnd.value = caret
  activeIndex.value = 0
  open.value = true
}

function choose(skill: AgentSkill) {
  emit('select', {
    skill,
    start: triggerStart.value,
    end: triggerEnd.value
  })
  close()
}

function handleKeydown(event: KeyboardEvent) {
  if (!open.value) return false
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
    return true
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    if (!filteredSkills.value.length) return true
    const direction = event.key === 'ArrowDown' ? 1 : -1
    activeIndex.value = (
      activeIndex.value + direction + filteredSkills.value.length
    ) % filteredSkills.value.length
    return true
  }
  if (event.key === 'Enter' && !event.isComposing) {
    event.preventDefault()
    const skill = filteredSkills.value[activeIndex.value]
    if (skill) choose(skill)
    return true
  }
  return false
}

function close() {
  open.value = false
  query.value = ''
  activeIndex.value = 0
}

defineExpose({ sync, handleKeydown, close })

onMounted(loadSkills)
</script>

<template>
  <div class="skill-slash-layer">
    <div v-if="selectedSkills.length" class="skill-selection" aria-label="本轮已选技能">
      <span v-for="skill in selectedSkills" :key="skill.name" class="skill-selection-chip">
        <span class="skill-selection-mark">/</span>
        <span>{{ skill.display_name }}</span>
        <button
          type="button"
          :aria-label="`移除${skill.display_name}`"
          :title="`移除${skill.display_name}`"
          :disabled="disabled"
          @click="emit('remove', skill.name)"
        >×</button>
      </span>
    </div>

    <section v-if="open" class="skill-slash-menu" aria-label="技能菜单">
      <header>
        <div>
          <strong>技能</strong>
          <span v-if="query">匹配“{{ query }}”</span>
          <span v-else>选择本轮能力</span>
        </div>
        <small>{{ selectedSkills.length }}/8</small>
      </header>

      <div v-if="loading" class="skill-menu-state">正在加载技能...</div>
      <div v-else-if="loadError" class="skill-menu-state error">{{ loadError }}</div>
      <div v-else-if="!filteredSkills.length" class="skill-menu-state">没有匹配的技能</div>
      <div v-else class="skill-menu-list" role="listbox">
        <button
          v-for="(skill, index) in filteredSkills"
          :key="skill.name"
          type="button"
          class="skill-menu-item"
          :class="{ active: index === activeIndex, selected: selectedNames.has(skill.name) }"
          role="option"
          :aria-selected="selectedNames.has(skill.name)"
          @mouseenter="activeIndex = index"
          @mousedown.prevent
          @click="choose(skill)"
        >
          <span class="skill-menu-icon">/</span>
          <span class="skill-menu-copy">
            <strong>{{ skill.display_name }}</strong>
            <small>{{ skill.summary }}</small>
          </span>
          <span class="skill-menu-meta">
            <em>{{ skill.availability === 'ready' ? '可用' : `${skill.readiness_percent}%` }}</em>
            <i v-if="selectedNames.has(skill.name)">已选</i>
          </span>
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.skill-slash-layer {
  display: contents;
}

.skill-selection {
  display: flex;
  gap: 6px;
  min-width: 0;
  overflow-x: auto;
  padding: 2px 3px 4px;
  scrollbar-width: thin;
}

.skill-selection-chip {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 5px;
  min-height: 27px;
  border: 1px solid rgba(43, 212, 212, 0.36);
  border-radius: 6px;
  background: rgba(7, 31, 32, 0.88);
  color: #c9f7f4;
  padding: 0 5px 0 8px;
  font-size: 12px;
}

.skill-selection-mark {
  color: #35d3cf;
  font-weight: 800;
}

.skill-selection-chip button {
  display: grid;
  width: 20px;
  height: 20px;
  place-items: center;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: #9db9b7;
  padding: 0;
  font-size: 16px;
  line-height: 1;
}

.skill-selection-chip button:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
}

.skill-slash-menu {
  position: absolute;
  z-index: 60;
  right: 0;
  bottom: calc(100% + 8px);
  left: 0;
  overflow: hidden;
  border: 1px solid rgba(213, 180, 106, 0.5);
  border-radius: 8px;
  background: #101011;
  box-shadow: 0 18px 50px rgba(0, 0, 0, 0.58);
}

.skill-slash-menu > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid rgba(213, 180, 106, 0.16);
  padding: 10px 12px;
}

.skill-slash-menu > header div {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: 9px;
}

.skill-slash-menu > header strong {
  color: #f3d88c;
  font-size: 13px;
}

.skill-slash-menu > header span,
.skill-slash-menu > header small {
  overflow: hidden;
  color: #88857d;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-menu-list {
  max-height: min(390px, 48vh);
  overflow-y: auto;
  padding: 5px;
}

.skill-menu-item {
  display: grid;
  width: 100%;
  grid-template-columns: 30px minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: #e6e1d4;
  padding: 8px;
  text-align: left;
}

.skill-menu-item.active {
  border-color: rgba(43, 212, 212, 0.3);
  background: rgba(10, 40, 41, 0.82);
}

.skill-menu-item.selected {
  background: rgba(48, 41, 20, 0.62);
}

.skill-menu-icon {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  border: 1px solid rgba(213, 180, 106, 0.34);
  border-radius: 6px;
  color: #e5c66f;
  font-weight: 800;
}

.skill-menu-copy {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.skill-menu-copy strong,
.skill-menu-copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-menu-copy strong {
  font-size: 13px;
}

.skill-menu-copy small {
  color: #9b988f;
  font-size: 11px;
}

.skill-menu-meta {
  display: flex;
  align-items: center;
  gap: 6px;
}

.skill-menu-meta em,
.skill-menu-meta i {
  border-radius: 999px;
  padding: 3px 6px;
  font-size: 10px;
  font-style: normal;
  white-space: nowrap;
}

.skill-menu-meta em {
  background: rgba(42, 198, 132, 0.12);
  color: #66dfab;
}

.skill-menu-meta i {
  background: rgba(213, 180, 106, 0.14);
  color: #e0c16c;
}

.skill-menu-state {
  padding: 30px 12px;
  color: #9d9a91;
  text-align: center;
}

.skill-menu-state.error {
  color: #ff8585;
}

@media (max-width: 680px) {
  .skill-slash-menu {
    right: -1px;
    left: -1px;
  }

  .skill-menu-list {
    max-height: 46vh;
  }

  .skill-menu-meta em {
    display: none;
  }
}
</style>
