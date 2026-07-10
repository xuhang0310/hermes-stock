<script setup lang="ts">
import AgentQueue from '../components/AgentQueue.vue'
import DataTable from '../components/DataTable.vue'
import type { DashboardPayload } from '../types'

defineProps<{
  dashboard: DashboardPayload
}>()

defineEmits<{
  seedDemo: []
}>()
</script>

<template>
  <div class="workbench-grid">
    <section class="summary-strip span-all">
      <div>
        <strong>{{ dashboard.counts.open_positions }}</strong>
        <span>持仓</span>
      </div>
      <div>
        <strong>{{ dashboard.counts.watchlist }}</strong>
        <span>自选</span>
      </div>
      <button type="button" @click="$emit('seedDemo')">插入样例数据</button>
    </section>

    <section class="page-section compact-section">
      <header class="section-header">
        <div>
          <h2>持仓台账</h2>
          <p>只读台账。</p>
        </div>
      </header>
      <DataTable
        :columns="['标的', '仓位', '止损', '状态']"
        :rows="dashboard.positions.map((item) => [
          item.symbol,
          `${item.position_pct}%`,
          item.stop_price ?? '-',
          item.status
        ])"
      />
    </section>

    <aside class="side-column">
      <section class="page-section compact-section">
        <header class="section-header">
          <div>
            <h2>Agent 队列</h2>
          </div>
        </header>
        <AgentQueue :agents="dashboard.agents" />
      </section>
    </aside>

    <section class="pipeline-strip span-all">
      <span v-for="(step, index) in dashboard.pipeline" :key="step">
        {{ step }}<template v-if="index < dashboard.pipeline.length - 1"> -> </template>
      </span>
    </section>
  </div>
</template>
