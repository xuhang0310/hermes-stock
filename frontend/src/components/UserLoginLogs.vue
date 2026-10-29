<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { UserLoginLog, UserLoginLogQuery } from '../types'
import { beijingFromDb } from '../utils/datetime'

const props = defineProps<{ username?: string }>()
const filters = reactive({ username: props.username || '', result: '', start_date: '', end_date: '' })
const items = ref<UserLoginLog[]>([])
const loading = ref(false)
const error = ref('')
const total = ref(0)
const page = ref(1)
const pageSize = 20
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const applied = ref<UserLoginLogQuery>({ username: props.username || undefined })
let requestId = 0

function loginReason(item: UserLoginLog) {
  if (item.success) return '身份验证通过'
  if (item.reason === 'bad_credentials') return '账号或密码错误，或账号未启用'
  if (item.reason === 'rate_limited') return '尝试次数过多，暂时限制登录'
  return item.reason || '登录未通过'
}

function roleLabel(role: string | null) {
  return ({ super_admin: '超级管理员', admin: '管理员', researcher: '研究用户', viewer: '只读用户' } as Record<string, string>)[role || ''] || '未验证身份'
}

async function loadPage(targetPage = page.value) {
  const currentRequest = ++requestId
  loading.value = true
  error.value = ''
  try {
    const result = await api.userLoginLogs({ ...applied.value, page: targetPage, page_size: pageSize })
    if (currentRequest !== requestId) return
    items.value = result.items
    total.value = result.total
    page.value = result.page
  } catch (err) {
    if (currentRequest !== requestId) return
    items.value = []
    total.value = 0
    error.value = err instanceof Error ? err.message : '登录日志加载失败'
  } finally {
    if (currentRequest === requestId) loading.value = false
  }
}

function search() {
  if (filters.start_date && filters.end_date && filters.start_date > filters.end_date) {
    error.value = '开始日期不能晚于结束日期'
    return
  }
  applied.value = {
    username: filters.username.trim() || undefined,
    success: filters.result === '' ? undefined : filters.result === 'success',
    start_date: filters.start_date || undefined,
    end_date: filters.end_date || undefined,
  }
  void loadPage(1)
}

function reset() {
  Object.assign(filters, { username: '', result: '', start_date: '', end_date: '' })
  search()
}

onMounted(() => { void loadPage(1) })
onUnmounted(() => { requestId += 1 })
</script>

<template>
  <section class="login-audit" aria-labelledby="login-audit-title">
    <header class="login-audit__header">
      <div>
        <h3 id="login-audit-title">登录记录</h3>
        <p>查看成功、失败及受限的登录尝试 · 按时间倒序 · 北京时间</p>
      </div>
      <button type="button" :disabled="loading" @click="loadPage()">{{ loading ? '加载中…' : '刷新记录' }}</button>
    </header>

    <form class="login-audit__filters" @submit.prevent="search">
      <label class="login-audit__username">用户名<input v-model="filters.username" maxlength="80" placeholder="输入完整用户名" autocomplete="off" /></label>
      <label>登录结果<select v-model="filters.result" aria-label="登录结果"><option value="">全部结果</option><option value="success">登录成功</option><option value="failure">登录失败 / 受限</option></select></label>
      <label>开始日期<input v-model="filters.start_date" type="date" :max="filters.end_date || undefined" /></label>
      <label>结束日期<input v-model="filters.end_date" type="date" :min="filters.start_date || undefined" /></label>
      <div class="login-audit__filter-actions"><button class="primary" type="submit" :disabled="loading">查询</button><button type="button" :disabled="loading" @click="reset">重置</button></div>
    </form>

    <p v-if="error" class="login-audit__error" role="alert">{{ error }}</p>
    <div class="login-audit__table-wrap" :aria-busy="loading">
      <table class="login-audit__table">
        <thead><tr><th scope="col">登录时间</th><th scope="col">用户</th><th scope="col">结果与原因</th><th scope="col">来源 IP</th><th scope="col">客户端</th></tr></thead>
        <tbody>
          <tr v-if="loading"><td colspan="5" class="login-audit__empty">正在加载登录记录…</td></tr>
          <tr v-else-if="error"><td colspan="5" class="login-audit__empty">未能加载记录，请重试。</td></tr>
          <tr v-else-if="!items.length"><td colspan="5" class="login-audit__empty">没有符合条件的登录记录</td></tr>
          <template v-else>
            <tr v-for="item in items" :key="item.id">
              <td><time :datetime="item.created_at || undefined">{{ beijingFromDb(item.created_at) }}</time><small>#{{ item.id }}</small></td>
              <td><b>{{ item.username || '空用户名' }}</b><small>{{ roleLabel(item.role) }}</small></td>
              <td><span class="login-audit__result" :class="item.success ? 'success' : 'failure'">{{ item.success ? '登录成功' : item.reason === 'rate_limited' ? '登录受限' : '登录失败' }}</span><small>{{ loginReason(item) }}</small></td>
              <td class="login-audit__ip">{{ item.ip || '未记录' }}</td>
              <td class="login-audit__client"><details v-if="item.user_agent"><summary :title="item.user_agent">{{ item.user_agent }}</summary><p>{{ item.user_agent }}</p></details><span v-else>未记录</span></td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
    <footer class="login-audit__footer">
      <span aria-live="polite">{{ loading ? '正在查询…' : error ? '查询未完成' : `共 ${total} 条记录` }}</span>
      <nav aria-label="登录日志分页">
        <button type="button" :disabled="loading || !!error || page <= 1" @click="loadPage(page - 1)">上一页</button>
        <span>{{ page }} / {{ pageCount }}</span>
        <button type="button" :disabled="loading || !!error || page >= pageCount" @click="loadPage(page + 1)">下一页</button>
      </nav>
    </footer>
  </section>
</template>

<style scoped>
.login-audit { color: #c8ced3; }
.login-audit__header { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin: 2px 0 20px; }
.login-audit__header h3 { margin: 0 0 6px; font-size: 17px; color: #e5e7eb; }
.login-audit__header p { margin: 0; color: #89949f; font-size: 12px; }
button { border: 1px solid #303842; border-radius: 7px; background: #151a1f; color: #d8dde2; padding: 9px 14px; cursor: pointer; white-space: nowrap; }
button.primary { border-color: #d5b45c; background: #d5b45c; color: #17130b; font-weight: 700; }
button:disabled { cursor: not-allowed; opacity: .5; }
.login-audit__filters { display: flex; align-items: end; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; padding: 16px; border: 1px solid #252c32; background: #11161a; border-radius: 9px; }
.login-audit__filters label { display: grid; gap: 7px; font-size: 12px; color: #9aa5ae; }
.login-audit__username { flex: 1; min-width: 180px; }
.login-audit__filters input, .login-audit__filters select { box-sizing: border-box; min-height: 38px; width: 100%; padding: 8px 10px; border: 1px solid #303840; border-radius: 6px; background: #090d10; color: #e4e8eb; font: inherit; color-scheme: dark; }
.login-audit :is(input, select, button, summary):focus-visible { outline: 2px solid #d5b45c; outline-offset: 3px; }
.login-audit__filter-actions { display: flex; gap: 8px; }
.login-audit__error { padding: 10px 12px; border-radius: 6px; border: 1px solid #64353b; background: #261519; color: #f08b96; }
.login-audit__table-wrap { overflow-x: auto; border: 1px solid #252c32; border-radius: 9px; }
.login-audit__table { width: 100%; min-width: 850px; border-collapse: collapse; font-size: 13px; }
.login-audit__table th { padding: 12px 16px; background: #11161a; color: #8f9aa3; text-align: left; font-size: 12px; font-weight: 500; }
.login-audit__table td { padding: 15px 16px; border-top: 1px solid #242a30; vertical-align: top; }
.login-audit__table tr:hover td { background: rgba(213, 180, 92, .025); }
.login-audit__table time, .login-audit__ip { white-space: nowrap; font-variant-numeric: tabular-nums; }
.login-audit__table b { font-weight: 500; color: #e1e6e9; }
.login-audit__table small { display: block; margin-top: 6px; color: #859099; font-size: 11px; }
.login-audit__result { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.login-audit__result::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.login-audit__result.success { color: #72c49a; }
.login-audit__result.failure { color: #f08b96; }
.login-audit__client { max-width: 260px; color: #89949f; font-size: 12px; }
.login-audit__client summary { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.login-audit__client p { margin: 8px 0 0; overflow-wrap: anywhere; line-height: 1.65; }
.login-audit__empty { padding: 48px 16px !important; text-align: center; color: #89949f; }
.login-audit__footer, .login-audit__footer nav { display: flex; align-items: center; gap: 14px; }
.login-audit__footer { justify-content: space-between; margin-top: 16px; color: #89949f; font-size: 12px; font-variant-numeric: tabular-nums; }
.login-audit__footer button { padding: 7px 11px; font-size: 12px; }
@media (max-width: 760px) {
  .login-audit__header { align-items: start; }
  .login-audit__filters label { flex: 1 1 calc(50% - 12px); min-width: 130px; }
  .login-audit__filters .login-audit__username { flex-basis: 100%; }
  .login-audit__filter-actions { width: 100%; justify-content: flex-end; }
}
</style>
