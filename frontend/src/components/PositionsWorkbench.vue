<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import DataTable from './DataTable.vue'
import type { BrokerPreset, PositionAccount, PositionItem, PositionSummary } from '../types'

const props = defineProps<{ positions: PositionItem[] }>()
const emit = defineEmits<{ refresh: []; openAgent: [accountId: number | null]; openChart: [position: PositionItem] }>()

const accounts = ref<PositionAccount[]>([])
const brokers = ref<BrokerPreset[]>([])
const summary = ref<PositionSummary | null>(null)
const selectedAccountId = ref<number | null>(null)
const message = ref('')

const accountModalOpen = ref(false)
const fundModalOpen = ref(false)
const editingAccountId = ref<number | null>(null)
const accountForm = reactive({ broker_code: 'manual', account_name: '', account_no_masked: '', is_default: false })
const fundForm = reactive({ account_id: 0, account_label: '', total_asset: null as number | null, holding_market_value: 0 })

const selectedAccount = computed(() => accounts.value.find((a) => a.id === selectedAccountId.value) || null)
const filteredPositions = computed(() =>
  selectedAccountId.value === null ? props.positions : props.positions.filter((p) => p.account_id === selectedAccountId.value)
)

function fmtMoney(v: number | null | undefined) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  return `¥${Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
}
function fmtNum(v: number | null | undefined, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '-'
  return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: digits })
}
function fmtPct(v: number | null | undefined) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '-'
  return `${Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}%`
}
function pnlClass(v: number | null | undefined) {
  if (v === null || v === undefined) return ''
  return v > 0 ? 'pnl-up' : v < 0 ? 'pnl-down' : ''
}

const fundCards = computed(() => {
  const a = selectedAccount.value
  const s = summary.value
  const src = a
    ? {
        total_asset: a.total_asset,
        available_cash: a.available_cash,
        holding_market_value: a.holding_market_value,
        total_pnl: a.total_pnl,
        day_pnl: a.day_pnl,
        holding_count: a.holding_count
      }
    : s
  if (!src) return []
  return [
    { label: '总资产', value: fmtMoney(src.total_asset), cls: '' },
    { label: '可用资金', value: fmtMoney(src.available_cash), cls: '' },
    { label: '持仓市值', value: fmtMoney(src.holding_market_value), cls: '' },
    { label: '累计浮盈', value: fmtMoney(src.total_pnl), cls: pnlClass(src.total_pnl) },
    { label: '当日盈亏', value: fmtMoney(src.day_pnl), cls: pnlClass(src.day_pnl) },
    { label: '持仓数', value: fmtNum(src.holding_count), cls: '' }
  ]
})

const tableColumns = computed(() =>
  selectedAccountId.value === null
    ? ['账户', '标的', '名称', '持仓', '现价', '市值', '浮盈', '盈亏率', '状态']
    : ['标的', '名称', '持仓', '可用', '成本', '现价', '市值', '浮盈', '盈亏率', '止损']
)
const tableRows = computed(() =>
  filteredPositions.value.map((p) =>
    selectedAccountId.value === null
      ? [
          p.broker_name || '—',
          p.symbol,
          p.name,
          fmtNum(p.position_quantity ?? p.quantity),
          fmtNum(p.latest_price, 2),
          fmtNum(p.market_value, 2),
          fmtNum(p.floating_pnl, 2),
          fmtPct(p.floating_pnl_pct),
          p.status
        ]
      : [
          p.symbol,
          p.name,
          fmtNum(p.position_quantity ?? p.quantity),
          fmtNum(p.available_quantity),
          fmtNum(p.cost_price, 2),
          fmtNum(p.latest_price, 2),
          fmtNum(p.market_value, 2),
          fmtNum(p.floating_pnl, 2),
          fmtPct(p.floating_pnl_pct),
          fmtNum(p.stop_price, 2)
        ]
  )
)

async function loadAccounts() {
  accounts.value = await api.positionAccounts()
  summary.value = await api.positionsSummary(selectedAccountId.value)
}
function selectAccount(id: number | null) {
  selectedAccountId.value = id
  void api.positionsSummary(id).then((s) => (summary.value = s))
}

function openNewAccount() {
  editingAccountId.value = null
  Object.assign(accountForm, { broker_code: brokers.value[0]?.code || 'manual', account_name: '', account_no_masked: '', is_default: false })
  message.value = ''
  accountModalOpen.value = true
}
function editAccount(a: PositionAccount) {
  editingAccountId.value = a.id
  Object.assign(accountForm, {
    broker_code: a.broker_code || 'manual',
    account_name: a.account_name || '',
    account_no_masked: a.account_no_masked || '',
    is_default: a.is_default
  })
  message.value = ''
  accountModalOpen.value = true
}
async function saveAccount() {
  message.value = ''
  try {
    if (editingAccountId.value) {
      await api.updatePositionAccount(editingAccountId.value, {
        account_name: accountForm.account_name || null,
        account_no_masked: accountForm.account_no_masked || null,
        is_default: accountForm.is_default
      })
    } else {
      const broker = brokers.value.find((b) => b.code === accountForm.broker_code)
      await api.createPositionAccount({
        broker_code: accountForm.broker_code,
        broker_name: broker?.name,
        account_name: accountForm.account_name || null,
        account_no_masked: accountForm.account_no_masked || null,
        is_default: accountForm.is_default
      })
    }
    accountModalOpen.value = false
    await loadAccounts()
  } catch (error) {
    message.value = `保存失败：${error instanceof Error ? error.message : String(error)}`
  }
}
async function archiveAccount(a: PositionAccount) {
  await api.updatePositionAccount(a.id, { status: 'archived' })
  if (selectedAccountId.value === a.id) selectedAccountId.value = null
  await loadAccounts()
}
async function setDefault(a: PositionAccount) {
  await api.updatePositionAccount(a.id, { is_default: true })
  await loadAccounts()
}

function openFund(a: PositionAccount) {
  fundForm.account_id = a.id
  fundForm.account_label = a.broker_name + (a.account_name ? ` · ${a.account_name}` : '')
  fundForm.total_asset = a.total_asset
  fundForm.holding_market_value = a.holding_market_value || 0
  message.value = ''
  fundModalOpen.value = true
}
const previewCash = computed(() =>
  fundForm.total_asset === null ? null : Number(fundForm.total_asset) - fundForm.holding_market_value
)
async function saveFund() {
  message.value = ''
  try {
    await api.updateAccountFunds(fundForm.account_id, fundForm.total_asset)
    fundModalOpen.value = false
    await loadAccounts()
    emit('refresh')
  } catch (error) {
    message.value = `保存失败：${error instanceof Error ? error.message : String(error)}`
  }
}

function onRowClick(index: number) {
  const position = filteredPositions.value[index]
  if (position) emit('openChart', position)
}

onMounted(async () => {
  brokers.value = await api.positionBrokers()
  await loadAccounts()
})
</script>

<template>
  <section class="positions-workbench">
    <div class="account-tabs">
      <button type="button" :class="{ active: selectedAccountId === null }" @click="selectAccount(null)">
        <span class="acct-name">全部账户</span>
        <span class="acct-sub">{{ summary ? fmtMoney(summary.total_asset) : '' }}</span>
      </button>
      <button
        v-for="a in accounts"
        :key="a.id"
        type="button"
        :class="{ active: selectedAccountId === a.id }"
        @click="selectAccount(a.id)"
      >
        <span class="acct-name">{{ a.broker_name }}<em v-if="a.is_default">默认</em></span>
        <span class="acct-sub">{{ fmtMoney(a.total_asset) }}<template v-if="a.account_no_masked"> · {{ a.account_no_masked }}</template></span>
      </button>
      <button type="button" class="acct-add" @click="openNewAccount">＋ 添加账户</button>
    </div>

    <div class="fund-grid">
      <div v-for="card in fundCards" :key="card.label" class="fund-card">
        <span class="fund-label">{{ card.label }}</span>
        <strong :class="card.cls">{{ card.value }}</strong>
      </div>
    </div>

    <section class="page-section">
      <header class="section-header">
        <div>
          <h2>{{ selectedAccount ? selectedAccount.broker_name + ' 持仓' : '全部持仓' }}</h2>
          <p>只读台账。点击持仓行查看 K 线图表。</p>
        </div>
        <div class="wb-actions">
          <button v-if="selectedAccount" type="button" @click="openFund(selectedAccount)">维护资金</button>
          <button v-if="selectedAccount" type="button" @click="editAccount(selectedAccount)">账户设置</button>
          <button type="button" @click="emit('openAgent', selectedAccountId)">添加持仓</button>
        </div>
      </header>
      <DataTable :columns="tableColumns" :rows="tableRows" clickable @row-click="onRowClick" />
    </section>

    <div v-if="accountModalOpen" class="wb-modal-backdrop" @click.self="accountModalOpen = false">
      <div class="wb-modal">
        <h3>{{ editingAccountId ? '账户设置' : '添加券商账户' }}</h3>
        <label v-if="!editingAccountId">
          <span>券商</span>
          <select v-model="accountForm.broker_code">
            <option v-for="b in brokers" :key="b.code" :value="b.code">{{ b.name }}</option>
          </select>
        </label>
        <label>
          <span>账户别名（可选）</span>
          <input v-model="accountForm.account_name" placeholder="如 主账户 / 两融" />
        </label>
        <label>
          <span>脱敏账号（可选）</span>
          <input v-model="accountForm.account_no_masked" placeholder="如 ****1234" />
        </label>
        <label class="wb-checkbox">
          <input v-model="accountForm.is_default" type="checkbox" />
          <span>设为默认账户</span>
        </label>
        <p v-if="message" class="form-message">{{ message }}</p>
        <div class="wb-modal-actions">
          <button
            v-if="editingAccountId && accounts.length > 1"
            type="button"
            class="danger"
            @click="editingAccountId && archiveAccount(accounts.find((x) => x.id === editingAccountId)!)"
          >
            删除账户
          </button>
          <button
            v-if="editingAccountId && !accounts.find((x) => x.id === editingAccountId)?.is_default"
            type="button"
            @click="editingAccountId && setDefault(accounts.find((x) => x.id === editingAccountId)!)"
          >
            设为默认
          </button>
          <span class="wb-spacer"></span>
          <button type="button" @click="accountModalOpen = false">取消</button>
          <button type="button" class="primary" @click="saveAccount">保存</button>
        </div>
      </div>
    </div>

    <div v-if="fundModalOpen" class="wb-modal-backdrop" @click.self="fundModalOpen = false">
      <div class="wb-modal">
        <h3>维护资金 · {{ fundForm.account_label }}</h3>
        <label>
          <span>总资产（券商 App 首页数字）</span>
          <input v-model.number="fundForm.total_asset" type="number" min="0" step="0.01" placeholder="如 100000" />
        </label>
        <div class="fund-preview">
          <span>当前持仓市值 {{ fmtMoney(fundForm.holding_market_value) }}</span>
          <span>可用资金（自动推算）<strong :class="pnlClass(previewCash)">{{ fmtMoney(previewCash) }}</strong></span>
        </div>
        <p class="fund-hint">可用资金 = 总资产 − 持仓市值，浮盈/当日盈亏由持仓自动汇总。</p>
        <p v-if="message" class="form-message">{{ message }}</p>
        <div class="wb-modal-actions">
          <span class="wb-spacer"></span>
          <button type="button" @click="fundModalOpen = false">取消</button>
          <button type="button" class="primary" @click="saveFund">保存资金</button>
        </div>
      </div>
    </div>
  </section>
</template>
