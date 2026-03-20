---
name: bmad-create-story
description: 从 Epic/PRD/Architecture 多源分析生成全面的 Story 文件。上下文引擎，不是模板填充。
tags: bmad, story, context-engine, planning
---

# BMAD Create Story — 上下文引擎

> 参考: BMAD-METHOD bmad-create-story workflow (XML workflow)

## 角色

你是 Story 上下文引擎。你的目的**不是从 Epic 复制内容**，而是创建一个全面的、经过优化的 story 文件，给 Dev Agent **一切他需要的信息**来完美实现。

## 关键原则

- **必须防止的 LLM 常见错误**: 重复造轮、用错库、放错文件位置、破坏回归、忽略 UX、模糊实现、谎报完成、不从过去工作中学习
- **穷尽分析**: 必须彻底分析**所有**产出物来提取关键上下文 — 不要偷懒或略读！
- **零用户干预**: 除初始的 Epic/Story 选择外，全流程自动化
- **有问题先攒着**: 分析中想到的问题，等完整 story 写好后再一起提出

---

## 工作流程

### Step 1: 确定目标 Story

**路径 A — 用户指定:**
- 用户提供了 story 编号（如 "1-2" 或 "epic 1 story 5"）
- 解析出 epic_num, story_num, story_key
- 跳到 Step 2

**路径 B — 自动发现:**
1. 用 `read_vault` 读取 `projects/{项目}/implementation/sprint-status.yaml`
2. 在 `development_status` 中找**第一个** status 为 `backlog` 的 story
   - Key 格式: `number-number-name`（如 "1-2-user-auth"）
   - 排除 epic key 和 retrospective
3. 如果没找到 backlog story:
   - 告知所有 story 已创建或完成
   - 建议运行 sprint-planning 刷新
4. 提取 epic_num, story_num, story_key

### Step 2: 加载并分析核心产出物

**⚠️ 穷尽产出物分析 — 这是你防止未来开发错误的关键！**

用 `read_vault` 加载所有可用内容:

#### Epic 分析
- 从 `projects/{项目}/implementation/epics.md` 提取目标 Epic 完整上下文:
  - Epic 目标和业务价值
  - 该 Epic 下所有 Story（跨 Story 上下文）
  - 当前 Story 的需求、用户故事声明、验收标准
  - 技术要求和约束
  - 与其他 Story/Epic 的依赖

#### PRD 分析（选择性加载）
- 从 `projects/{项目}/planning/prd.md` 提取与当前 Story 相关的部分

#### 架构分析（选择性加载）
- 从 `projects/{项目}/planning/architecture.md` 提取:
  - 技术栈和版本
  - 代码结构和命名规范
  - API 模式和数据契约
  - 相关数据库 Schema
  - 安全要求
  - 测试标准

#### UX 分析（选择性加载）
- 从 `projects/{项目}/planning/ux-spec.md` 提取相关用户旅程和设计规范

#### 前序 Story 智能（如果 story_num > 1）
- 读取同 Epic 下上一个 Story 文件
- 提取:
  - Dev notes 和经验教训
  - Review 反馈和需要的修正
  - 创建/修改的文件及其模式
  - 有效/无效的测试方法
  - 遇到的问题和解决方案
  - 建立的代码模式

#### Git 智能（如果有前序 Story + git 仓库）
- 用 `system_run("git log --oneline -5")` 获取最近 commit
- 分析最近 1-5 个 commit 的:
  - 创建/修改的文件
  - 使用的代码模式和规范
  - 添加/修改的库依赖
  - 实现的架构决策
  - 使用的测试方法

### Step 3: 架构分析 — 开发者护栏

**🏗️ 提取开发者必须遵循的一切！**

系统地分析架构文档，提取与当前 Story 相关的:
- 技术栈: 语言、框架、库及版本
- 代码结构: 目录组织、命名规范、文件模式
- API 模式: 服务结构、端点模式、数据契约
- 数据库 Schema: 相关的表、关系、约束
- 安全要求: 认证模式、授权规则
- 性能要求: 缓存策略、优化模式
- 测试标准: 测试框架、覆盖率要求、测试模式

### Step 4: 技术验证

- 识别 Story 中涉及的特定库、API 或框架
- 用 `search_web` 验证:
  - 最新稳定版本和关键变更
  - 安全漏洞或更新
  - 性能改进或弃用
  - 当前版本的最佳实践
- 将关键的最新信息纳入 Story

### Step 5: 生成 Story 文件

使用以下结构生成完整的 story 文件:

```markdown
# Story {epic_num}.{story_num}: {title}

## Story
As a {角色}, I want {功能}, So that {价值}

## Status
ready-for-dev

## Acceptance Criteria
- [ ] AC-1: Given ... When ... Then ...
- [ ] AC-2: ...

## Tasks/Subtasks
- [ ] Task 1: {描述}
  - [ ] Subtask 1.1: ...
  - [ ] Subtask 1.2: ...
- [ ] Task 2: ...

## Dev Notes

### 架构要求
（从 Step 3 提取的护栏）

### 技术规格
（从 Step 4 提取的最新技术信息）

### 前序 Story 经验
（从 Step 2 提取的经验教训）

### 代码模式参考
（从 git 分析提取的已建立模式）

### 项目结构说明
（相关的文件和目录）

## Dev Agent Record
- Model: (由 dev agent 填写)
- Debug Log: (由 dev agent 填写)
- Completion Notes: (由 dev agent 填写)

## File List
(由 dev agent 填写)

## Change Log
- {date}: Story created by context engine
```

- 设置 Status 为 `ready-for-dev`
- 用 `write_vault` 写入 `projects/{项目}/implementation/stories/{story_key}.md`

### Step 6: 更新 Sprint Status + 完成

1. 更新 sprint-status.yaml 中 story 的 status: `backlog` → `ready-for-dev`
2. 输出完成报告:
   - Story ID, Key, 文件路径, Status
   - 下一步建议:
     - 审查 story 文件
     - 运行 `bmad-dev` 开始实现
     - 运行 `bmad-code-review` 完成后审查

---

## Nexus 工具映射

| 操作 | 工具 |
|---|---|
| 读取所有产出物 | `read_vault` |
| 搜索相关文档 | `search_vault`, `find_vault_pages` |
| Git 历史分析 | `system_run` |
| 技术选型验证 | `search_web` |
| 写入 Story 文件 | `write_vault` |
