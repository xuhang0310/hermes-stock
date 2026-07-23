---
name: workspace-artifact-editing
description: 在多轮对话中持续读取、定位、修改和版本化工作区内的代码、Markdown、Word、Excel、PPT、PDF 源文件及其他研究产物。用户说“继续修改刚才的文件”“把上一版改一下”“在现有代码上调整”或上传文件要求编辑时使用，不应重新从头生成。
compatibility: Requires Hermes Stock artifact registry and workspace file tools; binary formats may require format-specific libraries.
metadata:
  hermes-display-name: 持续文件编辑
  hermes-category: artifact
  hermes-version: "3.0.0"
  hermes-risk-level: high
allowed-tools: ws_glob ws_read_file ws_edit_file ws_write_file
---

# 持续文件编辑

修改请求默认针对当前活动产物。先识别文件和版本，再做最小必要变更；重新生成只是无法安全编辑时的最后选择。

## 文件解析

1. 检查当前会话的活动产物和版本记录。
2. 用户明确提到文件名时优先精确匹配。
3. 有多个候选且会造成错误修改时，列出候选让用户确认。
4. 读取目标文件或其可编辑源文件，理解现有结构和风格。
5. 二进制文件需要找到对应的结构化编辑路径，不能把二进制内容当文本替换。

## 修改策略

- 文本和代码：使用定位明确的增量编辑。
- Markdown：保留标题层级、表格和引用结构。
- PPT：保留母版、尺寸、主题和未要求修改的页面。
- Word：保留样式、页眉页脚、目录和修订要求。
- Excel：保留公式、工作表结构、命名区域和现有格式。
- PDF：优先修改源文件后重新生成；没有源文件时说明限制。

详细生命周期见 [references/file-lifecycle.md](references/file-lifecycle.md)。

## 版本与安全

1. 修改前记录目标版本和哈希。
2. 不覆盖原始版本的可恢复历史。
3. 写入后重新读取或渲染验证关键内容。
4. 返回新版本、哈希、变更摘要和验证结果。
5. 外部路径、脚本执行和高风险写入遵守审批策略。

## 失败处理

定位不到原文件时不要静默新建同名替代品。说明缺少哪一项上下文，并提供恢复路径或让用户选择候选文件。
