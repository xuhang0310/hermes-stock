<script setup lang="ts">
defineProps<{
  columns: string[]
  rows: Array<Array<string | number | null>>
  clickable?: boolean
}>()

const emit = defineEmits<{
  rowClick: [index: number]
  cellClick: [rowIndex: number, cellIndex: number]
}>()
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
            @click="emit('cellClick', rowIndex, cellIndex)"
          ></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
