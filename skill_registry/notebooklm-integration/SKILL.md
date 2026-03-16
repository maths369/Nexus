---
name: NotebookLM Integration
description: 连接并操作 Google NotebookLM，用于上传文档、创建 notebook、提问和整理研究结果。
tags: notebooklm, google, integration, browser, research
---

# NotebookLM Integration

在任务涉及 **Google NotebookLM** 时使用这个 Skill。

## 适用任务

- 判断是否可以连接或操作 NotebookLM
- 登录 NotebookLM Web 界面
- 创建 notebook
- 上传 PDF / 文本 /文档资料
- 针对 notebook 内容提问并收集答案
- 把 NotebookLM 中的研究结果整理回 Vault

## 执行原则

1. **优先给出可执行结论，不要先说“我没有这个 capability”。**
2. NotebookLM 当前主要通过 **网页操作** 方式集成，而不是依赖公开稳定 API。
3. 如果浏览器工具可用，优先使用浏览器完成：
   - 打开 NotebookLM
   - 检查登录态
   - 创建 notebook / 上传资料 / 提问
4. 如果浏览器工具不可用，再明确说明当前缺的不是“能力概念”，而是“可用浏览器执行面”。
5. 如果用户只是问“是否可以连接 NotebookLM”，应回答：
   - **可以**，当前可以通过受管 Skill + 浏览器工作流连接和操作 NotebookLM
   - 但要明确：这通常是 Web 集成，不是官方 API 直连

## 推荐工作流

### 场景 1：用户问“能不能连接 NotebookLM”

默认回答方向：

- 可以连接
- 当前主路径是 **NotebookLM Web 集成**
- 如果需要，我可以继续：
  1. 打开 NotebookLM
  2. 创建或进入指定 notebook
  3. 上传文档
  4. 向 notebook 提问
  5. 把结果整理到 Vault

### 场景 2：用户要上传资料到 NotebookLM

1. 确认资料路径或最近上传附件
2. 如果浏览器工具可用：
   - 打开 NotebookLM
   - 进入目标 notebook
   - 上传文档
3. 成功后记录：
   - notebook 名称
   - 上传文件名
   - 时间
   - 结果摘要
4. 需要时写入 Vault

### 场景 3：用户要把 NotebookLM 结果沉淀到 Vault

1. 从页面提取问答 / 摘要 / 时间线 / 播客说明
2. 整理成结构化 Markdown
3. 存入：
   - `pages/`
   - `strategy/`
   - `research/`
   - 或用户指定目录

## 约束

- 不要把“没有官方 API”错误地表述成“完全不能连接”。
- 只要浏览器工作流可用，就应视为“可连接”。
- 如果当前环境没有浏览器执行面，要明确说明缺的是执行面，不是能力模型本身。
- 如果任务需要长期复用，可建议后续把 NotebookLM 集成沉淀为更稳定的正式扩展。
