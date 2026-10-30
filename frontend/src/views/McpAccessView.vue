<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { McpCredential, McpScope, UserPayload } from '../types'
import { beijingFromDb } from '../utils/datetime'

defineProps<{ user: UserPayload }>()

const scopeOptions: { key: McpScope; label: string; detail: string; write: boolean }[] = [
  { key: 'market:read', label: '行情分析', detail: '查询行情、个股与行业分析', write: false },
  { key: 'portfolio:read', label: '持仓分析', detail: '查看账户、持仓和风险依据', write: false },
  { key: 'schedules:read', label: '查看定时任务', detail: '查询任务与可用研究数字人', write: false },
  { key: 'portfolio:write', label: '录入持仓', detail: '新增买入记录，更新持仓账本', write: true },
  { key: 'schedules:write', label: '创建定时任务', detail: '新增定时研究任务', write: true }
]
const credentials = ref<McpCredential[]>([])
const availableScopes = ref<McpScope[]>([])
const endpointPath = ref('/api/mcp/')
const endpoint = computed(() => new URL(endpointPath.value, window.location.origin).href)
const loading = ref(true)
const loaded = ref(false)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const dialogError = ref('')
const dialog = ref<HTMLDialogElement | null>(null)
const revokeDialog = ref<HTMLDialogElement | null>(null)
const nameInput = ref<HTMLInputElement | null>(null)
const secretInput = ref<HTMLInputElement | null>(null)
const createdSecret = ref('')
const createdName = ref('')
const secretVisible = ref(false)
const copied = ref('')
const revokeTarget = ref<McpCredential | null>(null)
const revoking = ref(false)
const form = reactive({ name: '', scopes: [] as McpScope[], days: 90 })
let active = true
let loadVersion = 0
const options = computed(() => scopeOptions.filter((option) => availableScopes.value.includes(option.key)))
const activeCount = computed(() => credentials.value.filter((item) => credentialStatus(item) === '可用').length)
const canCreate = computed(() => loaded.value && !loading.value && options.value.length > 0)

function credentialStatus(item: McpCredential) {
  if (item.revoked_at) return '已撤销'
  if (item.is_invalidated) return '已失效'
  if (item.is_expired || Date.parse(item.expires_at) <= Date.now()) return '已过期'
  return '可用'
}
function scopeLabel(scope: McpScope) {
  return scopeOptions.find((item) => item.key === scope)?.label || scope
}
function connectionConfig(token = 'YOUR_MCP_TOKEN') {
  return JSON.stringify({ mcpServers: { 'sixrabbits-stock': {
    url: endpoint.value, headers: { Authorization: `Bearer ${token}` }
  } } }, null, 2)
}
async function load() {
  const version = ++loadVersion
  loading.value = true
  error.value = ''
  try {
    const result = await api.mcpTokens()
    if (!active || version !== loadVersion) return
    credentials.value = result.tokens
    availableScopes.value = result.available_scopes
    endpointPath.value = result.endpoint
    loaded.value = true
  } catch (err) {
    if (active && version === loadVersion) error.value = err instanceof Error ? err.message : '密钥列表加载失败，请重试。'
  } finally {
    if (active && version === loadVersion) loading.value = false
  }
}
async function openCreate() {
  if (!canCreate.value) return
  clearSecret()
  form.name = ''
  form.days = 90
  form.scopes = options.value.filter((item) => !item.write).map((item) => item.key)
  dialogError.value = ''
  dialog.value?.showModal()
  await nextTick()
  nameInput.value?.focus()
}
function clearSecret() {
  createdSecret.value = ''
  createdName.value = ''
  secretVisible.value = false
  copied.value = ''
}
function closeCreate() {
  if (saving.value) return
  dialog.value?.close()
  clearSecret()
}
async function createKey() {
  if (saving.value || createdSecret.value) return
  const name = form.name.trim()
  if (form.scopes.some((scope) => !availableScopes.value.includes(scope))) {
    dialogError.value = '所选权限已不可用，请刷新页面后重新选择。'
    return
  }
  const scopes = [...form.scopes]
  if (!name || !scopes.length || !Number.isInteger(form.days) || form.days < 1 || form.days > 365) {
    dialogError.value = '请填写密钥名称、至少一项权限，以及 1–365 天的有效期。'
    return
  }
  saving.value = true
  dialogError.value = ''
  notice.value = ''
  try {
    const result = await api.createMcpToken({ name, scopes, expires_in_days: form.days })
    if (!active) return
    // Keep the one-time secret in this component only. Refreshing the list must
    // never discard it or persist it in browser storage.
    createdSecret.value = result.token
    createdName.value = result.credential.name
    endpointPath.value = result.endpoint
    ++loadVersion
    loading.value = false
    credentials.value = [result.credential, ...credentials.value.filter((row) => row.id !== result.credential.id)]
  } catch (err) {
    if (active) dialogError.value = err instanceof Error ? err.message : '创建失败，请刷新列表确认后重试。'
  } finally {
    if (active) saving.value = false
  }
}
async function copyText(text: string, label: string, inDialog = false) {
  try {
    await navigator.clipboard.writeText(text)
    if (!active) return
    if (inDialog) copied.value = `${label}已复制`
    else notice.value = `${label}已复制`
  } catch {
    if (!active) return
    if (inDialog) {
      copied.value = '浏览器未允许复制，请显示密钥后选中手动复制。'
      secretVisible.value = true
      await nextTick()
      secretInput.value?.select()
    } else notice.value = '浏览器未允许复制，请选中地址或配置手动复制。'
  }
}
function requestRevoke(item: McpCredential) {
  if (item.revoked_at) return
  revokeTarget.value = item
  dialogError.value = ''
  revokeDialog.value?.showModal()
}
async function revokeKey() {
  if (!revokeTarget.value || revoking.value) return
  const item = revokeTarget.value
  revoking.value = true
  dialogError.value = ''
  try {
    await api.revokeMcpToken(item.id)
    if (!active) return
    ++loadVersion
    loading.value = false
    credentials.value = credentials.value.map((row) => row.id === item.id
      ? { ...row, revoked_at: new Date().toISOString() } : row)
    notice.value = `“${item.name}”已撤销，使用它的智能体将无法继续访问。`
    revokeDialog.value?.close()
    revokeTarget.value = null
  } catch (err) {
    if (active) dialogError.value = err instanceof Error ? err.message : '撤销失败，请重试。'
  } finally {
    if (active) revoking.value = false
  }
}
onMounted(load)
onUnmounted(() => { active = false; ++loadVersion; clearSecret() })
</script>

<template>
  <section class="mcp-access">
    <header class="mcp-heading">
      <div><span class="mcp-eyebrow">MCP ACCESS</span><h1>连接你的智能体</h1><p>让其他智能体调用行情、持仓与定时研究工具。</p></div>
      <button class="mcp-primary" :disabled="!canCreate" @click="openCreate"><span aria-hidden="true">＋</span> 创建密钥</button>
    </header>

    <section class="mcp-endpoint" aria-label="MCP 服务地址">
      <div class="mcp-connection-icon" aria-hidden="true">↗</div>
      <div class="mcp-endpoint-copy"><span>服务地址 <b>Streamable HTTP</b></span><code>{{ endpoint }}</code></div>
      <button class="mcp-button" @click="copyText(endpoint, '服务地址')">复制地址</button>
    </section>

    <p v-if="notice" class="mcp-notice" role="status">{{ notice }}</p>
    <div v-if="error" class="mcp-error" role="alert">{{ error }} <button class="mcp-link" :disabled="loading" @click="load">重试</button></div>

    <section class="mcp-keys" :aria-busy="loading">
      <header class="mcp-section-heading"><div><h2>我的密钥 <span>{{ credentials.length }}</span></h2><p>仅 {{ user.display_name || user.username }} 的账号可管理 · {{ activeCount }} 个可用</p></div><button class="mcp-button" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button></header>
      <div v-if="loading && !loaded" class="mcp-empty">正在加载密钥…</div>
      <div v-else-if="!credentials.length && !error" class="mcp-empty">
        <svg width="44" height="44" viewBox="0 0 32 32" fill="none" aria-hidden="true"><circle cx="11" cy="12" r="7" stroke="currentColor" stroke-width="1.5"/><path d="m16 17 11 11m-4-4 3-3m-7-1 3-3" stroke="currentColor" stroke-width="1.5"/></svg>
        <h3>创建第一个接入密钥</h3><p>为每个智能体分配独立密钥，按需选择它可以使用的功能。</p>
        <button class="mcp-primary" :disabled="!canCreate" @click="openCreate">创建密钥</button>
      </div>
      <div v-else-if="credentials.length" class="mcp-table-wrap">
        <table class="mcp-table"><thead><tr><th>名称 / 密钥</th><th>授权功能</th><th>创建时间</th><th>到期时间</th><th>状态</th><th class="mcp-last-cell">操作</th></tr></thead>
          <tbody><tr v-for="item in credentials" :key="item.id" :class="{ 'mcp-inactive': credentialStatus(item) !== '可用' }">
            <td><strong>{{ item.name }}</strong><code>{{ item.token_prefix }}••••••</code></td>
            <td><div class="mcp-scope-tags"><span v-for="scope in item.scopes" :key="scope" :class="{ 'mcp-write-tag': scope.endsWith(':write') }">{{ scopeLabel(scope) }}</span></div></td>
            <td class="mcp-time">{{ beijingFromDb(item.created_at) }}</td><td class="mcp-time">{{ beijingFromDb(item.expires_at) }}</td>
            <td><span class="mcp-status" :class="{ 'mcp-status-active': credentialStatus(item) === '可用' }"><i />{{ credentialStatus(item) }}</span></td>
            <td class="mcp-last-cell"><button v-if="!item.revoked_at" class="mcp-revoke" @click="requestRevoke(item)">撤销</button><span v-else class="mcp-muted">—</span></td>
          </tr></tbody>
        </table>
      </div>
      <p v-if="loaded && !availableScopes.length" class="mcp-footnote">当前账号没有可授权的功能，请联系管理员调整账号权限。</p>
    </section>

    <section class="mcp-guide">
      <div><span class="mcp-eyebrow">QUICK START</span><h2>三步完成接入</h2><p>密钥仅能访问你自己的持仓和任务。</p></div>
      <ol><li><b>01</b><div><strong>创建密钥</strong><span>填写名称，选择功能权限与有效期。</span></div></li><li><b>02</b><div><strong>复制连接配置</strong><span>创建成功后，复制包含密钥的配置。</span></div></li><li><b>03</b><div><strong>添加到智能体</strong><span>选择 HTTP MCP，填入地址和 Bearer 密钥。</span></div></li></ol>
    </section>
    <details class="mcp-config"><summary>查看通用连接配置</summary><p>客户端需支持自定义请求头；将 YOUR_MCP_TOKEN 替换为创建时保存的密钥。</p><pre>{{ connectionConfig() }}</pre><button class="mcp-button" @click="copyText(connectionConfig(), '配置模板')">复制配置模板</button></details>

    <dialog ref="dialog" class="mcp-dialog" aria-labelledby="mcp-create-title" @cancel="saving ? $event.preventDefault() : clearSecret()" @close="clearSecret">
      <template v-if="!createdSecret">
        <header><div><span class="mcp-eyebrow">NEW CONNECTION</span><h2 id="mcp-create-title">创建 MCP 密钥</h2></div><button class="mcp-close" aria-label="关闭" :disabled="saving" @click="closeCreate">×</button></header>
        <form @submit.prevent="createKey">
          <label class="mcp-field">密钥名称<input ref="nameInput" v-model="form.name" required maxlength="80" placeholder="例如：我的研究助手" :disabled="saving" autocomplete="off" /></label>
          <label class="mcp-field">有效期<select v-model.number="form.days" :disabled="saving"><option :value="7">7 天</option><option :value="30">30 天</option><option :value="90">90 天（推荐）</option><option :value="180">180 天</option><option :value="365">365 天</option></select></label>
          <fieldset :disabled="saving"><legend>允许使用的功能</legend><p>默认只读，录入持仓和创建任务需单独勾选。</p>
            <label v-for="option in options" :key="option.key" class="mcp-scope-option" :class="{ selected: form.scopes.includes(option.key) }"><input v-model="form.scopes" type="checkbox" :value="option.key" /><span><strong>{{ option.label }} <em v-if="option.write">可写入</em></strong><small>{{ option.detail }}</small></span></label>
          </fieldset>
          <p class="mcp-footnote">密钥原文仅在创建后展示一次，请及时保存。</p><p v-if="dialogError" class="mcp-error" role="alert">{{ dialogError }}</p>
          <footer><button type="button" class="mcp-button" :disabled="saving" @click="closeCreate">取消</button><button type="submit" class="mcp-primary" :disabled="saving || !form.name.trim() || !form.scopes.length">{{ saving ? '创建中…' : '创建密钥' }}</button></footer>
        </form>
      </template>
      <template v-else>
        <header><div><span class="mcp-eyebrow">CONNECTION READY</span><h2 id="mcp-create-title">密钥已创建</h2></div></header>
        <p class="mcp-created-copy">“{{ createdName }}”已准备就绪。请现在保存密钥，关闭后无法再次查看原文。</p>
        <label class="mcp-field">你的密钥<input ref="secretInput" :value="createdSecret" :type="secretVisible ? 'text' : 'password'" readonly autocomplete="off" spellcheck="false" /></label>
        <div class="mcp-secret-actions"><button class="mcp-link" @click="secretVisible = !secretVisible">{{ secretVisible ? '隐藏密钥' : '显示密钥' }}</button><button class="mcp-button" @click="copyText(createdSecret, '密钥', true)">复制密钥</button><button class="mcp-primary" @click="copyText(connectionConfig(createdSecret), '连接配置', true)">复制连接配置</button></div>
        <p class="mcp-copy-result" role="status">{{ copied || '配置中已包含服务地址和本次密钥，可直接粘贴到支持 HTTP MCP 的客户端。' }}</p>
        <footer><button class="mcp-button" @click="closeCreate">我已保存，关闭</button></footer>
      </template>
    </dialog>

    <dialog ref="revokeDialog" class="mcp-dialog mcp-revoke-dialog" aria-labelledby="mcp-revoke-title" @cancel="revoking && $event.preventDefault()">
      <header><h2 id="mcp-revoke-title">撤销密钥</h2></header><p>撤销“{{ revokeTarget?.name }}”后，使用它的智能体将无法继续访问。其他密钥不受影响。</p><p v-if="dialogError" class="mcp-error" role="alert">{{ dialogError }}</p><footer><button class="mcp-button" :disabled="revoking" @click="revokeDialog?.close()">取消</button><button class="mcp-danger" :disabled="revoking" @click="revokeKey">{{ revoking ? '撤销中…' : '确认撤销' }}</button></footer>
    </dialog>
  </section>
</template>

<style scoped>
.mcp-access { max-width: 1500px; margin: 0 auto; display: grid; gap: 24px; }
.mcp-heading, .mcp-section-heading, .mcp-dialog header { display: flex; justify-content: space-between; align-items: center; gap: 24px; }
.mcp-heading { padding: 12px 0 4px; }
.mcp-eyebrow { font-size: 10px; letter-spacing: .18em; color: var(--gold); font-weight: 600; }
.mcp-heading h1 { margin: 10px 0 8px; font-size: 28px; letter-spacing: -.03em; font-weight: 600; }
.mcp-heading p, .mcp-section-heading p, .mcp-guide p, .mcp-config p { margin: 6px 0 0; color: var(--muted); line-height: 1.7; font-size: 13px; }
.mcp-primary, .mcp-button, .mcp-danger { min-height: 36px; padding: 8px 15px; border-radius: 6px; white-space: nowrap; font-size: 13px; }
.mcp-primary { color: #211b0e; background: var(--gold); border: 1px solid var(--gold); font-weight: 600; }
.mcp-primary:hover:not(:disabled) { background: var(--gold-2); }
.mcp-primary > span { margin-right: 5px; font-size: 18px; }
.mcp-button { color: var(--text); background: transparent; border: 1px solid var(--line-strong); }
.mcp-button:hover:not(:disabled) { background: var(--surface-3); }
.mcp-access button:disabled { opacity: .45; cursor: not-allowed; }
.mcp-access button:focus-visible, .mcp-access summary:focus-visible { outline: 2px solid var(--gold); outline-offset: 3px; }
.mcp-endpoint { display: flex; align-items: center; gap: 16px; padding: 22px 24px; border: 1px solid var(--line-strong); border-radius: 8px; background: linear-gradient(100deg, #201c13, #111113 48%); }
.mcp-connection-icon { width: 40px; height: 40px; display: grid; place-items: center; color: var(--gold); font-size: 28px; border: 1px solid #645332; border-radius: 8px; }
.mcp-endpoint-copy { flex: 1; min-width: 0; }
.mcp-endpoint-copy > span { display: flex; align-items: center; gap: 12px; font-size: 12px; color: var(--muted); }
.mcp-endpoint-copy b { font-size: 10px; font-weight: 400; border: 1px solid var(--line); padding: 2px 6px; border-radius: 3px; }
.mcp-endpoint code { display: block; margin-top: 7px; font-size: 14px; overflow-wrap: anywhere; font-family: Consolas, monospace; }
.mcp-keys { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.mcp-section-heading { padding: 20px 24px; }
.mcp-section-heading h2, .mcp-guide h2 { margin: 0; font-size: 16px; font-weight: 600; }
.mcp-section-heading h2 > span { color: var(--muted); font-size: 12px; margin-left: 6px; font-weight: 400; }
.mcp-table-wrap { overflow-x: auto; }
.mcp-table { border-collapse: collapse; width: 100%; text-align: left; min-width: 880px; }
.mcp-table th { padding: 11px 20px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); background: #141416; color: var(--muted); font-weight: 400; font-size: 12px; }
.mcp-table td { padding: 18px 20px; border-bottom: 1px solid var(--line); font-size: 13px; vertical-align: middle; }
.mcp-table tbody tr:last-child td { border-bottom: 0; }
.mcp-table td:first-child { min-width: 190px; max-width: 280px; overflow-wrap: anywhere; }
.mcp-table td strong { font-weight: 500; display: block; }
.mcp-table td code { margin-top: 6px; display: block; font-size: 11px; color: var(--muted); font-family: Consolas, monospace; }
.mcp-scope-tags { display: flex; flex-wrap: wrap; gap: 5px; max-width: 260px; }
.mcp-scope-tags > span { padding: 3px 7px; font-size: 11px; border-radius: 4px; border: 1px solid var(--line); color: #bab8ad; white-space: nowrap; }
.mcp-scope-tags > .mcp-write-tag { border-color: #65502c; color: var(--gold); }
.mcp-time { white-space: nowrap; color: var(--muted); font-size: 12px !important; font-variant-numeric: tabular-nums; }
.mcp-status { display: inline-flex; gap: 6px; align-items: center; white-space: nowrap; color: var(--muted); font-size: 12px; }
.mcp-status i { height: 5px; width: 5px; background: currentColor; border-radius: 50%; }
.mcp-status-active { color: #75c4a6; }
.mcp-inactive td strong { color: var(--muted); }
.mcp-last-cell { text-align: right; }
.mcp-revoke, .mcp-link { background: transparent; border: 0; color: var(--gold); padding: 4px 0; font-size: 12px; }
.mcp-revoke { color: #d4877e; }
.mcp-muted { color: var(--muted); }
.mcp-empty { padding: 48px 24px; display: grid; justify-items: center; text-align: center; color: var(--muted); border-top: 1px solid var(--line); }
.mcp-empty svg { color: var(--gold-dim); margin-bottom: 12px; }
.mcp-empty h3 { font-size: 17px; margin: 8px 0; color: var(--text); font-weight: 500; }
.mcp-empty p { margin: 0 0 24px; font-size: 13px; line-height: 1.8; }
.mcp-guide { display: grid; grid-template-columns: 220px 1fr; gap: 32px; padding: 8px 0; }
.mcp-guide h2 { margin-top: 10px; }
.mcp-guide ol { display: grid; grid-template-columns: repeat(3, 1fr); margin: 0; padding: 0; list-style: none; gap: 24px; }
.mcp-guide li { display: flex; gap: 12px; align-items: flex-start; }
.mcp-guide li > b { color: var(--gold-dim); font-size: 20px; font-family: Georgia, serif; font-weight: 400; }
.mcp-guide strong { display: block; font-size: 13px; font-weight: 500; margin: 4px 0 8px; }
.mcp-guide li span { font-size: 12px; line-height: 1.8; color: var(--muted); }
.mcp-config { border-top: 1px solid var(--line); padding: 16px 0; font-size: 12px; color: var(--muted); }
.mcp-config summary { cursor: pointer; color: var(--text); }
.mcp-config pre { border: 1px solid var(--line); border-radius: 6px; padding: 16px; background: var(--surface); color: #bdb9ac; overflow-x: auto; line-height: 1.7; }
.mcp-notice, .mcp-error { margin: 0; padding: 12px 15px; border-radius: 5px; line-height: 1.7; font-size: 13px; }
.mcp-notice { color: #a7d7bf; border: 1px solid #294c3d; background: #122019; }
.mcp-error { color: #f5aaa4; border: 1px solid #633931; background: #281a18; }
.mcp-dialog { width: min(540px, calc(100vw - 32px)); max-height: calc(100dvh - 40px); padding: 28px; border: 1px solid var(--line-strong); border-radius: 12px; color: var(--text); background: #141415; box-shadow: 0 24px 90px #0009; }
.mcp-dialog::backdrop { background: #000a; backdrop-filter: blur(3px); }
.mcp-dialog header { margin-bottom: 24px; }
.mcp-dialog h2 { font-size: 21px; font-weight: 500; margin: 8px 0 0; }
.mcp-close { color: var(--muted); font-size: 25px; background: none; border: none; padding: 0 3px; }
.mcp-field { display: grid; gap: 8px; font-size: 13px; margin: 18px 0; }
.mcp-field input, .mcp-field select { min-height: 40px; padding: 10px 12px; width: 100%; color: var(--text); border: 1px solid var(--line-strong); border-radius: 5px; background: #0b0b0c; }
.mcp-field input:focus, .mcp-field select:focus { outline: 1px solid var(--gold); }
.mcp-field input[readonly] { font: 12px Consolas, monospace; }
.mcp-dialog fieldset { border: 0; padding: 0; margin: 24px 0 0; }
.mcp-dialog legend { padding: 0; font-size: 13px; }
.mcp-dialog fieldset > p { color: var(--muted); font-size: 12px; margin: 8px 0 12px; }
.mcp-scope-option { display: flex; align-items: center; gap: 12px; padding: 12px; border: 1px solid var(--line); border-radius: 5px; margin: 7px 0; cursor: pointer; }
.mcp-scope-option.selected { background: #252116; border-color: #65532f; }
.mcp-scope-option input { accent-color: var(--gold); height: 15px; width: 15px; margin: 0; }
.mcp-scope-option strong { font-size: 13px; font-weight: 500; }
.mcp-scope-option small { display: block; margin-top: 4px; color: var(--muted); font-size: 11px; }
.mcp-scope-option em { font-style: normal; font-size: 10px; color: var(--gold); margin-left: 6px; }
.mcp-footnote { color: var(--muted); font-size: 12px; line-height: 1.7; margin: 16px 0; }
.mcp-keys > .mcp-footnote { padding: 0 24px; }
.mcp-dialog footer { display: flex; gap: 10px; justify-content: flex-end; margin-top: 24px; padding-top: 18px; border-top: 1px solid var(--line); }
.mcp-secret-actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.mcp-created-copy, .mcp-revoke-dialog p { font-size: 13px; line-height: 1.9; color: var(--muted); }
.mcp-copy-result { min-height: 40px; color: var(--gold); font-size: 12px; line-height: 1.7; margin: 16px 0 0; }
.mcp-danger { background: #773d35; color: #ffe1dc; border: 1px solid #9a554b; }
@media (max-width: 1000px) { .mcp-guide { grid-template-columns: 1fr; gap: 18px; } }
@media (max-width: 600px) {
  .mcp-access { gap: 18px; }
  .mcp-heading { align-items: flex-start; gap: 14px; flex-wrap: wrap; }
  .mcp-heading h1 { font-size: 24px; }
  .mcp-endpoint { padding: 16px; gap: 12px; flex-wrap: wrap; }
  .mcp-connection-icon { display: none; }
  .mcp-endpoint-copy { flex-basis: 100%; }
  .mcp-section-heading { padding: 16px; gap: 12px; }
  .mcp-guide ol { grid-template-columns: 1fr; gap: 14px; }
  .mcp-dialog { padding: 20px; }
  .mcp-secret-actions { flex-wrap: wrap; }
  .mcp-secret-actions .mcp-link { width: 100%; text-align: left; }
}
</style>
