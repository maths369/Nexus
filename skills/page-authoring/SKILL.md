---
name: page-authoring
description: 以 Notion-style 方式创建和编辑页面，优先使用结构化 block 工具，而不是整页重写。
tags: document, notion, page, writing
---

# Page Authoring

在用户要创建页面、补充页面内容、插入结构化块、维护知识页面时，优先使用这个 skill。

## 目标

以结构化、可维护的方式编辑页面，而不是把整页 Markdown 直接整体覆盖。

## 使用原则

1. 能用 block 级工具完成时，不要优先用 `write_vault` 整页覆盖。
2. 新建页面用 `create_note`。
3. 对已有页面补充内容，优先用：
   - `document_append_block`
   - `document_replace_section`
4. 如果是待办清单，优先用 `document_insert_checklist`。
5. 如果是表格，优先用 `document_insert_table`。
6. 如果要建立页面关联，优先用 `document_insert_page_link`。

## 推荐调用

### 新建页面
```text
create_note(
  title="<标题>",
  body="<可选初始内容>",
  section="pages",
  page_type="note"
)
```

### 向指定 section 追加内容
```text
document_append_block(
  relative_path="<页面路径>",
  block_markdown="<要追加的 Markdown block>",
  heading="<可选 heading>"
)
```

### 替换某个 section
```text
document_replace_section(
  relative_path="<页面路径>",
  heading="<section 标题>",
  body="<新的 section 正文>",
  create_if_missing=true
)
```

## 何时澄清

1. 用户没给页面路径，也没说要新建还是编辑已有页面
2. 页面标题和 section 不明确
3. 用户想改的是整份正式文档，但没有说明保留哪些部分

## 输出要求

最终回复至少说明：
1. 操作的是哪一页
2. 执行了什么结构化编辑
3. 页面现在位于哪里

