<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api'
import type { StockCandidate } from '../types'

const props = withDefaults(
  defineProps<{
    modelValue: string | string[]
    multiple?: boolean
    placeholder?: string
    disabled?: boolean
    labels?: Record<string, string>
  }>(),
  { multiple: false, placeholder: '股票名称或代码', disabled: false, labels: () => ({}) }
)
const emit = defineEmits<{ 'update:modelValue': [value: string | string[]] }>()

type Tag = { code: string; name: string }

const root = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLInputElement | null>(null)
const open = ref(false)
const candidates = ref<StockCandidate[]>([])
const highlight = ref(0)
const noMatch = ref(false)

const displayText = ref('')
const tags = ref<Tag[]>([])

let searchTimer: ReturnType<typeof setTimeout> | null = null

function labelFor(code: string): string {
  return props.labels[code] || code
}

onMounted(() => {
  document.addEventListener('mousedown', onOutside)
  if (props.multiple) {
    tags.value = (props.modelValue as string[]).map((code) => ({ code, name: labelFor(code) }))
  } else {
    const v = (props.modelValue as string) || ''
    displayText.value = v && props.labels[v] ? `${props.labels[v]}  ${v}` : v
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onOutside)
  if (searchTimer) clearTimeout(searchTimer)
})

function onOutside(event: MouseEvent) {
  if (!root.value || root.value.contains(event.target as Node)) return
  open.value = false
}

function scheduleSearch(q: string) {
  if (searchTimer) clearTimeout(searchTimer)
  const key = q.trim()
  if (!key) {
    candidates.value = []
    open.value = false
    noMatch.value = false
    return
  }
  searchTimer = setTimeout(async () => {
    try {
      const res = await api.searchStocks(key)
      candidates.value = res.items
      highlight.value = 0
      noMatch.value = res.items.length === 0
      open.value = true
    } catch {
      candidates.value = []
      noMatch.value = true
      open.value = true
    }
  }, 220)
}

function pick(item: StockCandidate) {
  noMatch.value = false
  if (props.multiple) {
    if (!tags.value.some((t) => t.code === item.ts_code)) {
      tags.value.push({ code: item.ts_code, name: item.name || item.ts_code })
      emit('update:modelValue', tags.value.map((t) => t.code))
    }
    displayText.value = ''
  } else {
    displayText.value = item.name ? `${item.name}  ${item.ts_code}` : item.ts_code
    emit('update:modelValue', item.ts_code)
  }
  candidates.value = []
  open.value = false
}

function removeTag(index: number) {
  tags.value.splice(index, 1)
  emit('update:modelValue', tags.value.map((t) => t.code))
}

function onInput() {
  const v = displayText.value
  if (props.multiple) {
    scheduleSearch(v)
  } else {
    emit('update:modelValue', v.trim())
    scheduleSearch(v)
  }
  open.value = true
}

function onFocus() {
  if (candidates.value.length || noMatch.value) open.value = true
}

async function onKeydown(event: KeyboardEvent) {
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    if (candidates.value.length) highlight.value = (highlight.value + 1) % candidates.value.length
    return
  }
  if (event.key === 'ArrowUp') {
    event.preventDefault()
    if (candidates.value.length) highlight.value = (highlight.value - 1 + candidates.value.length) % candidates.value.length
    return
  }
  if (event.key !== 'Enter') return
  event.preventDefault()
  if (candidates.value.length) {
    pick(candidates.value[highlight.value] || candidates.value[0])
    return
  }
  const key = displayText.value.trim()
  if (!key) return
  try {
    const res = await api.searchStocks(key)
    if (res.items[0]) pick(res.items[0])
    else noMatch.value = true
  } catch {
    noMatch.value = true
  }
}
</script>

<template>
  <div ref="root" class="ssp" :class="{ 'ssp-multi': multiple }">
    <div class="ssp-tags" v-if="multiple && tags.length">
      <span v-for="(tag, i) in tags" :key="tag.code" class="ssp-tag">
        <span class="ssp-tag-name">{{ tag.name }}</span>
        <span class="ssp-tag-code" v-if="tag.name !== tag.code">{{ tag.code }}</span>
        <button type="button" class="ssp-tag-x" :disabled="disabled" @click="removeTag(i)">×</button>
      </span>
    </div>
    <input
      ref="inputEl"
      v-model="displayText"
      class="ssp-input"
      type="text"
      :placeholder="placeholder"
      :disabled="disabled"
      autocomplete="off"
      @input="onInput"
      @focus="onFocus"
      @keydown="onKeydown"
    />
    <div v-if="open && (candidates.length || noMatch)" class="ssp-menu">
      <button
        v-for="(item, i) in candidates"
        :key="item.ts_code"
        type="button"
        class="ssp-item"
        :class="{ active: i === highlight }"
        @mousedown.prevent="pick(item)"
      >
        <span class="ssp-item-name">{{ item.name || item.ts_code }}</span>
        <span class="ssp-item-code">{{ item.ts_code }}</span>
      </button>
      <div v-if="noMatch && !candidates.length" class="ssp-empty">无匹配，试试其它名称或代码</div>
    </div>
  </div>
</template>

<style scoped>
.ssp {
  position: relative;
  width: 100%;
}

.ssp-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-bottom: 6px;
}

.ssp-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 4px 2px 8px;
  border-radius: 999px;
  background: rgba(213, 180, 106, 0.1);
  border: 1px solid var(--line-strong);
  font-size: 11.5px;
}

.ssp-tag-name {
  color: var(--text);
}

.ssp-tag-code {
  color: var(--muted);
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: 10.5px;
}

.ssp-tag-x {
  border: none;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  padding: 0 3px;
  border-radius: 50%;
}

.ssp-tag-x:hover:not(:disabled) {
  color: var(--danger);
}

.ssp-tag-x:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.ssp-input {
  width: 100%;
  padding: 7px 10px;
  background: var(--bg-2);
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text);
  font-size: 12.5px;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.ssp-multi .ssp-input {
  font-family: inherit;
}

.ssp:not(.ssp-multi) .ssp-input {
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
}

.ssp-input:focus {
  border-color: var(--gold);
  box-shadow: 0 0 0 2px rgba(213, 180, 106, 0.14);
}

.ssp-input:disabled {
  opacity: 0.55;
}

.ssp-menu {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  z-index: 40;
  max-height: 220px;
  overflow-y: auto;
  background: var(--surface-3);
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  box-shadow: 0 12px 28px rgba(0, 0, 0, 0.5);
  padding: 4px;
}

.ssp-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 8px;
  border: none;
  background: transparent;
  border-radius: 4px;
  cursor: pointer;
  text-align: left;
}

.ssp-item:hover,
.ssp-item.active {
  background: rgba(213, 180, 106, 0.12);
}

.ssp-item-name {
  color: var(--text);
  font-size: 12.5px;
}

.ssp-item-code {
  color: var(--muted);
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: 11px;
}

.ssp-empty {
  padding: 8px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}
</style>
