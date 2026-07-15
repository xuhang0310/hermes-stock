<script setup lang="ts">
defineProps<{
  columns: string[]
  rows: Array<Array<string | number | null>>
  clickable?: boolean
}>()

const emit = defineEmits<{
  rowClick: [index: number]
  cellClick: [rowIndex: number, cellIndex: number, target: HTMLElement]
}>()

function onCellClick(e: MouseEvent, rowIndex: number, cellIndex: number) {
  const target = e.target as HTMLElement
  // 命中行内按钮（买入/卖出）时阻止冒泡到 rowClick，避免点按钮同时跳图表
  if (target.closest('button')) {
    e.stopPropagation()
    emit('cellClick', rowIndex, cellIndex, target.closest('button') as HTMLElement)
  }
}
</script>

<template>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="rows.length === 0">
          <td :colspan="columns.length" class="empty-cell">暂无记录</td>
        </tr>
        <tr
          v-for="(row, rowIndex) in rows"
          :key="rowIndex"
          :class="{ 'clickable-row': clickable }"
          :tabindex="clickable ? 0 : undefined"
          @click="clickable && emit('rowClick', rowIndex)"
          @keydown.enter="clickable && emit('rowClick', rowIndex)"
        >
          <td
            v-for="(cell, cellIndex) in row"
            :key="cellIndex"
            v-html="cell"
            @click="(e) => onCellClick(e, rowIndex, cellIndex)"
          ></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
