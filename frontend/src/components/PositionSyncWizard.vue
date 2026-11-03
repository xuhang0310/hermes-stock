<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type {
  PositionAccount,
  PositionSyncApplyResult,
  PositionSyncItem,
  PositionSyncItemKind,
  PositionSyncPreview,
  PositionSyncRecognizeResult,
  PositionSyncRow
} from '../types'

// 持仓同步向导：选账户 → 拖入截图（可多张）→ 核对变化 → 一次确认 → 可撤销。
// 同步是“以截图为准的校正”，不会生成交易记录；只有勾选“这是一笔买入/卖出”的行才记成交。

const props = defineProps<{ accounts: PositionAccount[]; defaultAccountId: number | null }>()
const emit = defineEmits<{ close: []; synced: [accountId: number] }>()

type Step = 'upload' | 'recognizing' | 'review' | 'saving' | 'done'
type SyncImage = { id: string; name: string; mime: string; dataUrl: string }
type TradeDraft = { enabled: boolean; action: 'buy' | 'sell'; quantity: number | null; price: number | null }

const LAST_ACCOUNT_KEY = 'sixrabbits:positions:sync-account:v1'
const MAX_IMAGES = 6
const MAX_BYTES = 8 * 1024 * 1024

const step = ref<Step>('upload')
const images = ref<SyncImage[]>([])
const dragging = ref(false)
const accountId = ref<number | null>(initialAccountId())
const error = ref('')
const elapsed = ref(0)
let timer: ReturnType<typeof setInterval> | null = null

const recognized = ref<PositionSyncRecognizeResult | null>(null)
const rows = ref<PositionSyncRow[]>([])
const preview = ref<PositionSyncPreview | null>(null)
const previewLoading = ref(false)
let previewRequest = 0
const decisions = reactive<Record<string, boolean>>({})
const trades = reactive<Record<string, TradeDraft>>({})
const fixDrafts = reactive<Record<string, string>>({})
const fixing = ref<string | null>(null)
const applyFunds = ref(true)
const showUnchanged = ref(false)
const result = ref<PositionSyncApplyResult | null>(null)
const undoing = ref(false)
const undoMessage = ref('')

const busy = computed(() => step.value === 'recognizing' || step.value === 'saving' || undoing.value)

function initialAccountId(): number | null {
  const ids = new Set(props.accounts.map((account) => account.id))
  if (props.defaultAccountId && ids.has(props.defaultAccountId)) return props.defaultAccountId
  try {
    const saved = Number(window.localStorage.getItem(LAST_ACCOUNT_KEY))
    if (saved && ids.has(saved)) return saved
  } catch {
    // 浏览器禁用存储时忽略
  }
  return props.accounts.find((account) => account.is_default)?.id ?? null
}

function accountLabel(id: number | null | undefined) {
  const account = props.accounts.find((item) => item.id === id)
  return account ? account.broker_name + (account.account_name ? ` · ${account.account_name}` : '') : '未选择'
}

function fmtQty(value: number | null | undefined) {
  return value == null ? '-' : value.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

function fmtMoney(value: number | null | undefined) {
  return value == null ? '-' : `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function errorText(err: unknown) {
  return err instanceof Error ? err.message : String(err)
}

// ---------------------------------------------------------------- 选图
function readAsDataUrl(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.readAsDataURL(file)
  })
}

async function addFiles(files: File[]) {
  error.value = ''
  for (const file of files) {
    if (!file.type.startsWith('image/')) {
      error.value = `“${file.name}”不是图片，已跳过。`
      continue
    }
    if (file.size > MAX_BYTES) {
      error.value = `“${file.name}”超过 8MB，请压缩后再传。`
      continue
    }
    if (images.value.length >= MAX_IMAGES) {
      error.value = `一次最多 ${MAX_IMAGES} 张截图。`
      break
    }
    images.value.push({
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      name: file.name || `截图 ${images.value.length + 1}`,
      mime: file.type || 'image/png',
      dataUrl: await readAsDataUrl(file)
    })
  }
}

function onFileInput(event: Event) {
  const input = event.target as HTMLInputElement
  void addFiles(Array.from(input.files || []))
  input.value = ''
}

function onDrop(event: DragEvent) {
  dragging.value = false
  void addFiles(Array.from(event.dataTransfer?.files || []))
}

function onPaste(event: ClipboardEvent) {
  if (step.value !== 'upload') return
  const files = Array.from(event.clipboardData?.items || [])
    .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file))
  if (files.length) {
    event.preventDefault()
    void addFiles(files)
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}

function removeImage(id: string) {
  images.value = images.value.filter((image) => image.id !== id)
}

// ---------------------------------------------------------------- 识别与对比
function selectAccount(id: number) {
  if (busy.value) return
  accountId.value = id
  try {
    window.localStorage.setItem(LAST_ACCOUNT_KEY, String(id))
  } catch {
    // 忽略
  }
  if (step.value === 'review') void loadPreview()
}

function resetDecisions(next: PositionSyncPreview | null) {
  for (const key of Object.keys(decisions)) delete decisions[key]
  for (const key of Object.keys(trades)) delete trades[key]
  if (!next) return
  for (const item of next.items) {
    decisions[item.key] = item.apply
    trades[item.key] = {
      enabled: false,
      action: item.trade_hint?.action ?? 'sell',
      quantity: item.trade_hint?.quantity ?? null,
      price: item.trade_hint?.price ?? null
    }
  }
  applyFunds.value = next.apply_funds_default
}

async function recognize() {
  if (!images.value.length) return
  error.value = ''
  step.value = 'recognizing'
  elapsed.value = 0
  timer = setInterval(() => (elapsed.value += 1), 1000)
  try {
    const data = await api.positionSyncRecognize({
      images: images.value.map((image) => ({ image_base64: image.dataUrl, mime_type: image.mime, name: image.name })),
      account_id: accountId.value
    })
    recognized.value = data
    rows.value = data.rows
    if (!accountId.value && data.detected_account_id) accountId.value = data.detected_account_id
    preview.value = data.preview
    resetDecisions(data.preview)
    step.value = 'review'
    if (!data.preview && accountId.value) await loadPreview()
  } catch (err) {
    error.value = errorText(err)
    step.value = 'upload'
  } finally {
    if (timer) clearInterval(timer)
    timer = null
  }
}

async function loadPreview() {
  if (!accountId.value || !recognized.value) return
  const request = ++previewRequest
  const targetAccountId = accountId.value
  previewLoading.value = true
  preview.value = null
  resetDecisions(null)
  error.value = ''
  try {
    const next = await api.positionSyncPreview({
      account_id: targetAccountId,
      rows: rows.value,
      funds: recognized.value.funds
    })
    if (request !== previewRequest || targetAccountId !== accountId.value || step.value !== 'review') return
    preview.value = next
    resetDecisions(next)
  } catch (err) {
    if (request === previewRequest) error.value = errorText(err)
  } finally {
    if (request === previewRequest) previewLoading.value = false
  }
}

async function fixInvalid(item: PositionSyncItem) {
  const symbol = (fixDrafts[item.key] || '').trim()
  if (!symbol) return
  fixing.value = item.key
  error.value = ''
  try {
    const validation = await api.validatePositionAgentSymbol({ symbol, name: item.name })
    if (!validation.valid) {
      error.value = `${item.name}：${validation.message}`
      return
    }
    rows.value = rows.value.map((row) =>
      row.key === item.key
        ? {
            ...row,
            key: validation.normalized_symbol,
            symbol: validation.normalized_symbol,
            name: validation.canonical_name || row.name,
            valid: true,
            corrected: false,
            validation_status: validation.status,
            validation_message: validation.message
          }
        : row
    )
    await loadPreview()
  } catch (err) {
    error.value = errorText(err)
  } finally {
    fixing.value = null
  }
}

function backToUpload() {
  if (busy.value) return
  previewRequest += 1
  previewLoading.value = false
  step.value = 'upload'
  recognized.value = null
  preview.value = null
  rows.value = []
  error.value = ''
}

// ---------------------------------------------------------------- 分组与汇总
const GROUPS: Array<{ kind: PositionSyncItemKind; title: string; hint: string }> = [
  { kind: 'invalid', title: '代码需要确认', hint: '改正代码后会加入清单；不改就不同步这几行' },
  { kind: 'new', title: '新增', hint: '系统里还没有' },
  { kind: 'changed', title: '有变化', hint: '数量或成本和系统里不一样' },
  { kind: 'missing', title: '截图里没有', hint: '勾选表示已经清仓' }
]

const groups = computed(() =>
  GROUPS.map((group) => ({ ...group, items: (preview.value?.items || []).filter((item) => item.kind === group.kind) })).filter(
    (group) => group.items.length
  )
)
const unchanged = computed(() => (preview.value?.items || []).filter((item) => item.kind === 'unchanged'))
const failedImages = computed(() => (recognized.value?.images || []).filter((image) => !image.ok))
const warnings = computed(() => [...(recognized.value?.warnings || []), ...(preview.value?.warnings || [])])
const detectedId = computed(() => recognized.value?.detected_account_id ?? null)
const detectedMismatch = computed(() => Boolean(detectedId.value && accountId.value && detectedId.value !== accountId.value))
const fundsUsable = computed(() => preview.value?.funds_after?.available_cash != null)

const selection = computed(() => {
  const picked = { new: 0, changed: 0, missing: 0, trades: 0 }
  for (const item of preview.value?.items || []) {
    if (!decisions[item.key]) continue
    if (item.kind === 'new' || item.kind === 'changed' || item.kind === 'missing') picked[item.kind] += 1
    if (item.trade_hint && trades[item.key]?.enabled) picked.trades += 1
  }
  return picked
})

const tradeDraftsValid = computed(() =>
  (preview.value?.items || []).every((item) => {
    const draft = trades[item.key]
    if (!decisions[item.key] || !item.trade_hint || !draft?.enabled) return true
    return Boolean(draft.quantity && draft.quantity > 0 && draft.price && draft.price > 0)
  })
)

const summaryText = computed(() => {
  const parts: string[] = []
  if (selection.value.new) parts.push(`新增 ${selection.value.new} 只`)
  if (selection.value.changed) parts.push(`更新 ${selection.value.changed} 只`)
  if (selection.value.missing) parts.push(`清仓 ${selection.value.missing} 只`)
  if (selection.value.trades) parts.push(`记 ${selection.value.trades} 笔成交`)
  if (applyFunds.value && fundsUsable.value) parts.push('更新可用资金')
  return parts.length ? `将${parts.join('、')}` : '没有要同步的变化'
})

const canApply = computed(
  () =>
    Boolean(
      step.value === 'review' && accountId.value && preview.value?.account_id === accountId.value &&
      !previewLoading.value && tradeDraftsValid.value
    ) &&
    (selection.value.new + selection.value.changed + selection.value.missing > 0 || (applyFunds.value && fundsUsable.value))
)

// ---------------------------------------------------------------- 确认与撤销
async function confirm() {
  if (!canApply.value || !preview.value || !accountId.value) return
  step.value = 'saving'
  error.value = ''
  const items = preview.value.items
    .filter((item) => item.kind === 'new' || item.kind === 'changed' || item.kind === 'missing')
    .map((item) => {
      const draft = trades[item.key]
      return {
        kind: item.kind,
        symbol: item.symbol,
        name: item.name,
        apply: Boolean(decisions[item.key]),
        after: item.after,
        trade: item.trade_hint && draft?.enabled ? { enabled: true, action: draft.action, quantity: draft.quantity, price: draft.price } : null
      }
    })
  try {
    result.value = await api.positionSyncApply({
      account_id: accountId.value,
      items,
      funds: recognized.value?.funds ?? null,
      apply_funds: applyFunds.value && fundsUsable.value,
      as_of_date: recognized.value?.as_of_date ?? null,
      image_sha256: recognized.value?.image_sha256 ?? null,
      account_no_masked: recognized.value?.account_no_masked ?? null
    })
    step.value = 'done'
    emit('synced', accountId.value)
  } catch (err) {
    error.value = errorText(err)
    step.value = 'review'
  }
}

async function undo() {
  if (!result.value || undoing.value) return
  if (!window.confirm('撤销这次同步？持仓和可用资金会回到同步前。')) return
  undoing.value = true
  error.value = ''
  try {
    const undone = await api.positionSyncUndo(result.value.batch_id)
    undoMessage.value = undone.message
    emit('synced', undone.account.id)
  } catch (err) {
    error.value = errorText(err)
  } finally {
    undoing.value = false
  }
}

function close() {
  if (busy.value) return
  emit('close')
}

onMounted(() => {
  window.addEventListener('paste', onPaste)
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  previewRequest += 1
  window.removeEventListener('paste', onPaste)
  window.removeEventListener('keydown', onKeydown)
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="wb-modal-backdrop" @click.self="close">
    <div class="wb-modal sync-wizard" role="dialog" aria-modal="true" aria-labelledby="sync-wizard-title">
      <header class="sync-head">
        <div>
          <h3 id="sync-wizard-title">同步持仓</h3>
          <p>以券商截图为准，先核对变化再写入；同步不会生成交易记录。</p>
        </div>
        <button type="button" class="sync-close" :disabled="busy" aria-label="关闭" @click="close">×</button>
      </header>

      <section v-if="step !== 'done'" class="sync-accounts" aria-label="选择账户">
        <span class="sync-label">账户</span>
        <div class="sync-chips">
          <button
            v-for="account in accounts"
            :key="account.id"
            type="button"
            :class="{ active: account.id === accountId }"
            :aria-pressed="account.id === accountId"
            :disabled="busy"
            @click="selectAccount(account.id)"
          >
            {{ account.broker_name }}<small v-if="account.account_no_masked">{{ account.account_no_masked }}</small>
          </button>
        </div>
      </section>
      <p v-if="step === 'review' && detectedMismatch" class="sync-banner warn">
        截图看起来是<b>{{ accountLabel(detectedId) }}</b>（{{ recognized?.detected_reason }}），你选的是{{ accountLabel(accountId) }}。
        <button type="button" class="sync-link" @click="selectAccount(detectedId as number)">切换到{{ accountLabel(detectedId) }}</button>
      </p>
      <p v-else-if="step === 'review' && detectedId && detectedId === accountId" class="sync-banner ok">
        已按{{ recognized?.detected_reason }}认出是{{ accountLabel(accountId) }}。
      </p>

      <section v-if="step === 'upload'" class="sync-upload">
        <label
          class="sync-drop"
          :class="{ dragging }"
          @dragenter.prevent="dragging = true"
          @dragover.prevent="dragging = true"
          @dragleave.prevent="dragging = false"
          @drop.prevent="onDrop"
        >
          <input type="file" accept="image/*" multiple @change="onFileInput" />
          <strong>拖入、选择或粘贴券商持仓截图</strong>
          <span>同一账户往下滑的几屏可以一起放，系统会自动合并去重；最多 {{ MAX_IMAGES }} 张，每张不超过 8MB。</span>
        </label>
        <ul v-if="images.length" class="sync-thumbs">
          <li v-for="image in images" :key="image.id">
            <img :src="image.dataUrl" :alt="image.name" />
            <button type="button" :aria-label="`移除 ${image.name}`" @click="removeImage(image.id)">×</button>
          </li>
        </ul>
      </section>

      <section v-else-if="step === 'recognizing'" class="sync-progress" aria-live="polite" aria-busy="true">
        <span class="sync-spinner" aria-hidden="true"></span>
        <div>
          <strong>正在读取 {{ images.length }} 张截图…</strong>
          <span>已用 {{ elapsed }} 秒，一般 20–40 秒。识别完先给你核对，不会直接写入。</span>
        </div>
      </section>

      <section v-else-if="step === 'review' || step === 'saving'" class="sync-review">
        <p v-if="!accountId" class="sync-banner warn">没认出是哪个账户，请在上面选一个。</p>
        <p v-if="failedImages.length" class="sync-banner warn">
          有 {{ failedImages.length }} 张没识别出来：{{ failedImages.map((image) => image.name).join('、') }}。可以点“重新选图”换一张。
        </p>
        <p v-for="(warning, index) in warnings" :key="index" class="sync-banner warn">{{ warning }}</p>
        <div v-if="previewLoading" class="sync-loading">正在和系统持仓对比…</div>
        <template v-else-if="preview">
          <div v-if="preview.funds_after" class="sync-funds">
            <label class="sync-check">
              <input v-model="applyFunds" type="checkbox" :disabled="!fundsUsable || step === 'saving'" />
              <span>用截图里的资金更新{{ fundsUsable ? '' : '（截图里没读到可用资金）' }}</span>
            </label>
            <dl>
              <div>
                <dt>可用资金</dt>
                <dd>{{ fmtMoney(preview.funds_before.available_cash) }} → <b>{{ fmtMoney(preview.funds_after.available_cash) }}</b></dd>
              </div>
              <div>
                <dt>总资产（截图）</dt>
                <dd>{{ fmtMoney(preview.funds_after.total_asset) }}</dd>
              </div>
              <div>
                <dt>持仓市值（截图）</dt>
                <dd>{{ fmtMoney(preview.funds_after.holding_market_value) }}</dd>
              </div>
            </dl>
          </div>

          <div v-for="group in groups" :key="group.kind" class="sync-group" :class="`kind-${group.kind}`">
            <h4>{{ group.title }} <small>{{ group.items.length }} · {{ group.hint }}</small></h4>
            <ul>
              <li v-for="item in group.items" :key="item.key" class="sync-item">
                <div class="sync-item-main">
                  <label class="sync-check">
                    <input
                      v-if="group.kind !== 'invalid'"
                      v-model="decisions[item.key]"
                      type="checkbox"
                      :disabled="step === 'saving'"
                    />
                    <span class="sync-name">{{ item.name }}<code v-if="item.symbol">{{ item.symbol }}</code></span>
                  </label>
                  <span class="sync-change">{{ item.changes.join('；') }}</span>
                </div>
                <p v-if="item.note" class="sync-note">{{ item.note }}</p>
                <div v-if="group.kind === 'invalid'" class="sync-fix">
                  <input v-model.trim="fixDrafts[item.key]" placeholder="输入正确的 6 位代码" :aria-label="`${item.name} 的正确代码`" />
                  <button type="button" :disabled="fixing === item.key || !fixDrafts[item.key]" @click="fixInvalid(item)">
                    {{ fixing === item.key ? '校验中…' : '改正' }}
                  </button>
                </div>
                <div v-else-if="item.trade_hint && decisions[item.key] && trades[item.key]" class="sync-trade">
                  <label class="sync-check">
                    <input v-model="trades[item.key].enabled" type="checkbox" :disabled="step === 'saving'" />
                    <span>这是一笔{{ trades[item.key].action === 'buy' ? '买入' : '卖出' }}，记进交易记录</span>
                  </label>
                  <template v-if="trades[item.key].enabled">
                    <input v-model.number="trades[item.key].quantity" type="number" min="1" step="100" aria-label="成交股数" />
                    <span>股，成交价</span>
                    <input v-model.number="trades[item.key].price" type="number" min="0" step="0.001" aria-label="成交价" />
                    <span>元</span>
                  </template>
                </div>
              </li>
            </ul>
          </div>

          <p v-if="!groups.length && !unchanged.length" class="sync-muted">截图里没有读到持仓明细。</p>
          <button v-if="unchanged.length" type="button" class="sync-link" @click="showUnchanged = !showUnchanged">
            {{ showUnchanged ? '收起' : '展开' }}没有变化的 {{ unchanged.length }} 只
          </button>
          <ul v-if="showUnchanged" class="sync-unchanged">
            <li v-for="item in unchanged" :key="item.key">{{ item.name }} · {{ fmtQty(item.after?.position_quantity) }} 股</li>
          </ul>
        </template>
      </section>

      <section v-else-if="step === 'done' && result" class="sync-done" aria-live="polite">
        <p class="sync-done-title">{{ undoMessage ? '已撤销' : '同步完成' }}</p>
        <p>{{ undoMessage || result.message }}</p>
        <p v-if="result.learned_account_no && !undoMessage" class="sync-muted">
          已记住这个账户的账号 {{ result.learned_account_no }}，下次能自动认出。
        </p>
        <p v-for="(warning, index) in result.warnings" :key="index" class="sync-banner warn">{{ warning }}</p>
      </section>

      <p v-if="error" class="sync-banner error" role="alert">{{ error }}</p>

      <footer class="wb-modal-actions">
        <template v-if="step === 'upload'">
          <span class="sync-muted">{{ images.length ? `已选 ${images.length} 张` : '' }}</span>
          <span class="wb-spacer"></span>
          <button type="button" @click="close">取消</button>
          <button type="button" class="primary" :disabled="!images.length" @click="recognize">开始识别</button>
        </template>
        <template v-else-if="step === 'review' || step === 'saving'">
          <button type="button" :disabled="busy" @click="backToUpload">重新选图</button>
          <span class="sync-summary">{{ summaryText }}</span>
          <span class="wb-spacer"></span>
          <button type="button" class="primary" :disabled="!canApply || step === 'saving'" @click="confirm">
            {{ step === 'saving' ? '同步中…' : '确认同步' }}
          </button>
        </template>
        <template v-else-if="step === 'done'">
          <button v-if="!undoMessage" type="button" class="danger" :disabled="undoing" @click="undo">
            {{ undoing ? '撤销中…' : '撤销这次同步' }}
          </button>
          <span class="wb-spacer"></span>
          <button type="button" class="primary" @click="emit('close')">完成</button>
        </template>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.sync-wizard {
  max-width: 760px;
  max-height: min(88vh, 900px);
  overflow-y: auto;
  gap: 14px;
}
.sync-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.sync-head p {
  margin: 4px 0 0;
  color: var(--muted-2);
  font-size: 12px;
}
.sync-close {
  border: 0;
  background: transparent;
  color: var(--muted);
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
}
.sync-accounts {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.sync-label {
  color: var(--muted);
  font-size: 12px;
}
.sync-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.sync-chips button {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  padding: 6px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  background: #111;
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
}
.sync-chips button small {
  color: var(--muted-2);
  font-size: 11px;
}
.sync-chips button.active {
  border-color: var(--gold);
  background: rgba(213, 180, 106, 0.16);
  color: var(--gold-2);
}
.sync-banner {
  margin: 0;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
}
.sync-banner.warn {
  border: 1px solid rgba(224, 160, 80, 0.4);
  background: rgba(224, 160, 80, 0.08);
  color: #e8b878;
}
.sync-banner.ok {
  border: 1px solid rgba(37, 211, 180, 0.35);
  background: rgba(37, 211, 180, 0.07);
  color: #7fe3cf;
}
.sync-banner.error {
  border: 1px solid rgba(255, 93, 93, 0.4);
  background: rgba(255, 93, 93, 0.08);
  color: var(--danger);
}
.sync-link {
  margin-left: 6px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--gold-2);
  font-size: 12px;
  text-decoration: underline;
  cursor: pointer;
}
.sync-upload {
  display: grid;
  gap: 12px;
}
.sync-drop {
  position: relative;
  display: grid;
  gap: 6px;
  padding: 28px 18px;
  border: 1px dashed var(--line-strong);
  border-radius: 10px;
  background: #0d0d0e;
  text-align: center;
  cursor: pointer;
}
.sync-drop.dragging {
  border-color: var(--gold);
  background: rgba(213, 180, 106, 0.06);
}
.sync-drop input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}
.sync-drop strong {
  color: var(--text);
  font-size: 14px;
}
.sync-drop span {
  color: var(--muted-2);
  font-size: 12px;
}
.sync-thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.sync-thumbs li {
  position: relative;
  width: 84px;
  height: 120px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 6px;
}
.sync-thumbs img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.sync-thumbs button {
  position: absolute;
  top: 3px;
  right: 3px;
  width: 22px;
  height: 22px;
  border: 0;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.7);
  color: #fff;
  cursor: pointer;
}
.sync-progress {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 24px 8px;
}
.sync-progress div {
  display: grid;
  gap: 4px;
}
.sync-progress strong {
  color: var(--text);
}
.sync-progress span {
  color: var(--muted-2);
  font-size: 12px;
}
.sync-spinner {
  width: 26px;
  height: 26px;
  flex: none;
  border: 3px solid var(--line-strong);
  border-top-color: var(--gold);
  border-radius: 50%;
  animation: sync-spin 0.9s linear infinite;
}
@keyframes sync-spin {
  to {
    transform: rotate(360deg);
  }
}
.sync-review {
  display: grid;
  gap: 12px;
}
.sync-loading,
.sync-muted {
  color: var(--muted-2);
  font-size: 12px;
}
.sync-funds {
  display: grid;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #0d0d0e;
}
.sync-funds dl {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 8px;
  margin: 0;
}
.sync-funds dt {
  color: var(--muted-2);
  font-size: 11px;
}
.sync-funds dd {
  margin: 2px 0 0;
  color: var(--text);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.sync-group {
  padding-left: 10px;
  border-left: 3px solid var(--line-strong);
}
.sync-group.kind-new {
  border-left-color: #25d3b4;
}
.sync-group.kind-changed {
  border-left-color: var(--gold);
}
.sync-group.kind-missing {
  border-left-color: var(--danger);
}
.sync-group.kind-invalid {
  border-left-color: #e0a050;
}
.sync-group h4 {
  margin: 0 0 6px;
  color: var(--text);
  font-size: 13px;
}
.sync-group h4 small {
  margin-left: 6px;
  color: var(--muted-2);
  font-weight: 400;
}
.sync-group ul,
.sync-unchanged {
  display: grid;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.sync-item {
  display: grid;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #0f0f10;
}
.sync-item-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.sync-wizard label.sync-check {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--text);
  font-size: 13px;
}
.sync-wizard label.sync-check input {
  width: auto;
  height: auto;
}
.sync-name code {
  margin-left: 6px;
  color: var(--muted-2);
  font-size: 11px;
}
.sync-change {
  color: var(--gold-2);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.sync-note {
  margin: 0;
  color: #e8b878;
  font-size: 11px;
}
.sync-fix,
.sync-trade {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  color: var(--muted);
  font-size: 12px;
}
.sync-fix input,
.sync-trade input[type='number'] {
  width: 120px;
  height: 30px;
}
.sync-fix button {
  padding: 5px 12px;
  border: 1px solid var(--gold-dim);
  border-radius: 6px;
  background: #111;
  color: var(--gold-2);
  cursor: pointer;
}
.sync-unchanged li {
  color: var(--muted-2);
  font-size: 12px;
}
.sync-done {
  display: grid;
  gap: 6px;
}
.sync-done p {
  margin: 0;
  color: var(--text);
  font-size: 13px;
}
.sync-done .sync-done-title {
  color: var(--gold-2);
  font-size: 16px;
  font-weight: 600;
}
.sync-summary {
  color: var(--muted);
  font-size: 12px;
}
@media (max-width: 640px) {
  .sync-wizard {
    max-height: 94vh;
    padding: 16px;
  }
  .sync-item-main {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
