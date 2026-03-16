---
name: meeting-minutes
description: 使用 Notion-style 文档块生成和维护会议纪要，包含摘要、行动项、追踪表和关联页面。
tags: meetings, document, notion, minutes
---

# Meeting Minutes

在用户要整理会议纪要、更新会后行动项、维护会议追踪页时，优先使用这个 skill。

## 推荐结构

1. `## 摘要`
2. `## 行动项`
3. `## 追踪表`
4. `## 参考`

## 推荐工具顺序

1. 没有页面时：
   - `create_note`
2. 写摘要：
   - `document_replace_section`
3. 插入行动项：
   - `document_insert_checklist`
4. 插入追踪表：
   - `document_insert_table`
5. 引用其它页面或数据库：
   - `document_insert_page_link`

## 追踪表示例

headers:
- 任务
- 负责人
- 截止时间
- 状态

## 何时和音频技能联动

如果用户给的是会议录音，不要直接写纪要正文，先加载 `meeting-transcription`，完成转录和初步物化后，再回到本 skill 补摘要、行动项和追踪表。

