---
name: vault-page-management
description: 在 Vault 中列出页面、排查重名页、确认页面路径、删除或清理重复页面时使用。
tags:
  - vault
  - document
  - cleanup
  - duplicate
---

# Vault Page Management

这个 skill 处理的是 **Vault 页面层**，不是普通工作区文件系统。

## 适用场景

- 用户要求列出当前有哪些 PDF / 日志 / 会议纪要页面
- 用户说“有两个同名文档，帮我删掉一个”
- 用户要求删除误创建页面
- 用户要求确认某个页面的真实相对路径

## 必须优先使用的工具

1. `find_vault_pages`
   - 按标题、相对路径或 page_id 查找页面
   - 适合重名页面排查

2. `list_vault_pages`
   - 按 section 列出页面
   - 适合盘点 PDF、日志、会议纪要等

3. `read_vault`
   - 读取页面内容做确认

4. `delete_page`
   - 删除前会自动创建备份

## 禁止的错误做法

- 不要优先用 `list_local_files` 来盘点 Vault 页面
- 不要把 `pages/`、`journals/`、`meetings/` 当成项目源码目录
- 不要在重名页面场景下直接猜测要删哪一个

## 推荐流程

### 场景 A：用户说“列出现在你已经有的 PDF 文件”

1. 先判断这是 Vault 页面盘点，不是工作区文件系统查询
2. 用 `list_vault_pages(section="inbox")` 和必要的其他分区盘点
3. 过滤标题或内容里与 PDF 导入页相关的页面
4. 返回标题 + 相对路径

### 场景 B：用户说“现在有两个‘日志 2026-03-11’文档，帮我删掉一个”

1. 先用 `find_vault_pages(query="日志 2026-03-11")`
2. 把所有候选页面列出来，包含：
   - `title`
   - `relative_path`
   - `updated_at`
3. 如果用户没有明确说保留哪一个，就先澄清
4. 只有拿到明确路径后，再调用 `delete_page(relative_path=...)`

## 回复要求

- 页面盘点要列出真实 `relative_path`
- 删除操作前要说明会自动备份
- 删除完成后要回报被删除的路径
