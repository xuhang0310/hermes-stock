<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{
  columns: string[]
  rows: Array<Array<string | number | null>>
  clickable?: boolean
  reorderable?: boolean
  fixedColumnIndexes?: number[]
}>()

const emit = defineEmits<{
  rowClick: [index: number]
  cellClick: [rowIndex: number, cellIndex: number, target: HTMLElement]
  columnReorder: [fromIndex: number, toIndex: number]
}>()

const draggedColumn = ref<number | null>(null)
const dragOverColumn = ref<number | null>(null)

function onCellClick(e: MouseEvent, rowIndex: number, cellIndex: number) {
  const target = e.target as HTMLElement
  // 命中行内按钮（买入/卖出）时阻止冒泡到 rowClick，避免点按钮同时跳图表
  if (target.closest('button')) {
    e.stopPropagation()
    emit('cellClick', rowIndex, cellIndex, target.closest('button') as HTMLElement)
  }
}

function isFixedColumn(index: number) {
  return props.fixedColumnIndexes?.includes(index) ?? false
}

function isDraggableColumn(index: number) {
  return Boolean(props.reorderable && !isFixedColumn(index))
}

function onColumnDragStart(event: DragEvent, index: number) {
  if (!isDraggableColumn(index)) {
    event.preventDefault()
    return
  }
  draggedColumn.value = index
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', String(index))
  }
}

function onColumnDragOver(event: DragEvent, index: number) {
  if (draggedColumn.value === null) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  dragOverColumn.value = isFixedColumn(index) ? Math.max(0, index - 1) : index
}

function onColumnDrop(event: DragEvent, index: number) {
  event.preventDefault()
  const fromIndex = draggedColumn.value
  const toIndex = isFixedColumn(index) ? Math.max(0, index - 1) : index
  if (fromIndex !== null && fromIndex !== toIndex) {
    emit('columnReorder', fromIndex, toIndex)
  }
  draggedColumn.value = null
  dragOverColumn.value = null
}

function onColumnDragEnd() {
  draggedColumn.value = null
  dragOverColumn.value = null
}
</script>

<template>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th
            v-for="(column, columnIndex) in columns"
            :key="column"
            :draggable="isDraggableColumn(columnIndex)"
            :class="{
              'column-reorderable': isDraggableColumn(columnIndex),
              'column-dragging': draggedColumn === columnIndex,
              'column-drag-over': dragOverColumn === columnIndex
            }"
            @dragstart="onColumnDragStart($event, columnIndex)"
            @dragover="onColumnDragOver($event, columnIndex)"
            @drop="onColumnDrop($event, columnIndex)"
            @dragend="onColumnDragEnd"
          >
            <div class="table-column-heading">
              <span
                v-if="isDraggableColumn(columnIndex)"
                class="table-column-drag-handle"
                title="拖动调整列位置"
                aria-hidden="true"
              >⠿</span>
              <span>{{ column }}</span>
            </div>
          </th>
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
