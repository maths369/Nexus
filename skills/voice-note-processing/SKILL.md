---
name: voice-note-processing
description: 处理日常语音笔记和灵感记录，转录后整理为 Vault 页面或知识条目。
tags: audio, voice-note, transcription, knowledge-capture
---

# Voice Note Processing

在用户提到语音笔记、灵感记录、随手语音、采访草稿时，优先使用这个 skill。

## 目标

把零散语音内容转成后续可编辑、可检索、可沉淀的知识页面，而不是停留在临时 transcript。

## 使用原则

1. 如果用户给了本地音频路径，优先调用 `audio_transcribe_path`。
2. 默认 `materialize=true`，并根据内容语义优先写入：
   - `pages`
   - `notes`
   - `inbox`
3. 如果用户给的是已有转录文本，调用 `audio_materialize_transcript`。
4. 输出时重点强调：
   - 保存到了哪里
   - 是否适合进一步整理为正式文档
   - 是否值得写入长期记忆

## 推荐调用方式

### A. 本地语音文件

```text
audio_transcribe_path(
  audio_path="<本地音频文件路径>",
  target_section="pages",
  title="<标题，可选>",
  materialize=true,
  language="<zh/en，可选>"
)
```

### B. 已有转录文本

```text
audio_materialize_transcript(
  source_name="<来源描述>",
  transcript="<转录正文>",
  summary="<一句话摘要，可选>",
  action_items=[],
  target_section="pages",
  title="<标题，可选>"
)
```

## 何时追加后处理

如果语音内容明显包含以下类型，可以在转录后继续建议用户：

1. 待办事项
2. 项目决策
3. 个人偏好
4. 研究想法

对应下一步可以使用：

1. 写入 Vault 页面
2. 提炼行动项
3. 归档到长期记忆

## 何时先澄清

1. 音频路径不存在
2. 用户没说明要不要保存到 Vault
3. 内容明显属于会议纪要，应改用 `meeting-transcription`

