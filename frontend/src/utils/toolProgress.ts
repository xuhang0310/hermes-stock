import type { AgentStreamEvent } from '../types'

/** 工具参数生成期的瞬态进度（写文件/长参数时模型静默期的可视化），action 到达即清。 */
export type LiveToolProgress = {
  index: number
  displayName: string
  chars: number
  preview: string
}

// 预览只保留尾部：框体高度有界、新内容始终可见，无需滚动逻辑
const PREVIEW_TAIL = 1500

/** 累积 tool_progress 事件；index 变化视为模型开启新一个工具调用，预览重置。 */
export function applyToolProgress(prev: LiveToolProgress | null | undefined, event: AgentStreamEvent): LiveToolProgress {
  const index = event.index ?? 0
  const sameCall = !!prev && prev.index === index
  const preview = ((sameCall ? prev!.preview : '') + (event.delta || '')).slice(-PREVIEW_TAIL)
  return {
    index,
    displayName: event.display_name || event.tool || '工具',
    chars: event.chars || 0,
    preview
  }
}
