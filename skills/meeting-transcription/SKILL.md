---
name: meeting-transcription
description: 处理会议录音转录、整理纪要、提取行动项，并物化到 Vault 的 meetings 分区。
tags: audio, meetings, transcription, vault
---

# Meeting Transcription

在用户提到会议录音、会议纪要、会后整理、语音会议总结时，优先使用这个 skill。

## 目标

把会议音频转换为可检索、可追踪、可继续编辑的知识资产，而不是只返回一段裸文本。

## 使用原则

1. 先确认是否已有本地音频文件路径。
2. 如果用户提供的是音频文件，优先调用 `audio_transcribe_path`。
3. 默认 `materialize=true`，并把结果写入 `meetings` 分区。
4. 如果用户已经有转录文本而不是音频文件，调用 `audio_materialize_transcript`。
5. 转录完成后，给出：
   - 生成的页面路径
   - 原始 transcript 路径
   - 是否识别出行动项
   - 建议的下一步（如继续整理会议纪要、拆解待办）

## 推荐调用方式

### A. 用户提供本地音频文件

调用：

```text
audio_transcribe_path(
  audio_path="<本地音频文件路径>",
  target_section="meetings",
  title="<会议标题，可选>",
  materialize=true,
  language="<zh/en，可选>"
)
```

### B. 用户只提供转录文本

调用：

```text
audio_materialize_transcript(
  source_name="<源文件名或来源描述>",
  transcript="<转录正文>",
  summary="<可选摘要>",
  action_items=["<待办1>", "<待办2>"],
  target_section="meetings",
  title="<会议标题，可选>"
)
```

## 何时先澄清

以下情况先问清楚，不要直接执行：

1. 没有音频路径，也没有转录文本
2. 用户没说清楚是会议录音还是普通语音笔记
3. 用户希望保存到非 `meetings` 分区
4. 用户希望输出特定模板（例如周会纪要、1:1 纪要、技术评审纪要）

## 输出要求

最终回复至少包含：

1. 转录是否完成
2. 生成的 Vault 页面位置
3. 原始 transcript 位置
4. 是否有摘要 / 行动项
5. 后续可继续做的事情
   - 提炼会议纪要
   - 提取责任人
   - 生成任务清单

