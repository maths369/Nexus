---
name: BMAD Code Review (三层对抗审查)
description: 三层并行对抗式代码审查：Blind Hunter（纯 diff）+ Edge Case Hunter（diff + 项目代码）+ Acceptance
  Auditor（diff + spec）。自动去重、分类、呈现。
tags:
- bmad
- review
- adversarial
- code-quality
keywords:
- 代码审查
- review
- code review
- 审查
- 对抗审查
- 代码质量
---

# BMAD Code Review — 三层对抗审查

> 参考: BMAD-METHOD bmad-code-review workflow (step-01 ~ step-04)

## 概述

这不是普通的代码审查。它使用**三个独立的审查视角**并行分析代码变更，然后去重、分类、呈现。每个审查者都被设定为极其严苛的角色，被要求"找出问题"而非"确认正确"。

---

## 工作流程

### Step 1: 收集上下文

**确定审查范围** — 按用户指定或自动检测：

1. 从用户消息推断审查模式：
   - "staged" / "暂存的改动" → 仅暂存变更
   - "uncommitted" / "所有改动" → 未提交变更（staged + unstaged）
   - "branch diff" / "vs main" → 分支差异
   - "这个 diff" / "这段代码" → 用户提供的 diff
2. 如果无法推断，**询问用户**:
   - 未提交的变更（staged + unstaged）
   - 仅暂存的变更
   - 分支差异（需指定 base branch）
   - 特定 commit 范围
   - 用户提供的 diff 或文件列表

3. 用 `system_run` 构建 diff：
   - 分支差异: `git diff {base}...HEAD`
   - 未提交: `git diff HEAD`
   - 暂存: `git diff --cached`
   - 验证 diff 非空，否则告知用户无内容可审查

4. 询问用户: **是否有对应的 spec 或 story 文件？**
   - 有 → `review_mode = "full"`，用 `read_vault` 加载 spec
   - 无 → `review_mode = "no-spec"`

5. 如果 diff 超过 3000 行，警告用户并提供分块选项

6. **检查点**: 展示 diff 统计（文件数、增删行数）、review_mode、已加载的上下文文档。等待用户确认。

---

### Step 2: 三层并行审查

使用 `dispatch_subagent` 并行启动三个独立审查者。**每个审查者不共享会话上下文**。

#### 🔴 Blind Hunter（盲审者）

**System prompt:**
```
你是一个愤世嫉俗的代码审查者，对粗糙的工作零容忍。
你只看到 diff，没有任何项目上下文、spec 或文档。
用极度怀疑的态度审查 — 假设问题存在。
找出至少 10 个需要修复或改进的问题。
输出: Markdown 列表，每项一个问题描述。
如果找到 0 个问题 — 这很可疑，重新分析。
```

**输入**: 仅 diff_output
**不给**: 任何项目上下文、spec、文档

#### 🟡 Edge Case Hunter（边界猎手）

**System prompt:**
```
你是一个机械式的路径追踪者，专找未处理的边界情况。
你可以看到 diff 和项目代码。
对每个发现，提供:
- location: 文件和行号
- trigger_condition: 触发条件
- guard_snippet: 建议的防护代码
- potential_consequence: 如果不修复会怎样
输出: JSON 数组格式。
```

**输入**: diff_output + 可通过工具读取项目代码
**工具**: `code_read_file`, `list_local_files`

#### 🟢 Acceptance Auditor（验收审计员）— 仅在 review_mode="full" 时

**System prompt:**
```
你是验收审计员。将 diff 与 spec 和上下文文档进行对照审查。
检查: 违反验收标准、偏离 spec 意图、缺少的指定行为、spec 约束与实际代码的矛盾。
每个发现: 一行标题 + 违反的 AC/约束 + diff 中的证据。
输出: Markdown 列表格式。
```

**输入**: diff_output + spec 内容 + 上下文文档

**失败处理**: 如果某个审查者失败/超时/返回空结果，记录该层失败，继续处理其他层的结果。

---

### Step 3: 分类（Triage）

收集所有审查结果后，主 Agent 执行分类：

#### 3.1 标准化
将三个审查者的不同格式统一为:
- `id`: 序号
- `source`: `blind` / `edge` / `auditor` / 合并来源如 `blind+edge`
- `title`: 一行摘要
- `detail`: 完整描述
- `location`: 文件和行号（如有）

#### 3.2 去重
如果两个或以上发现描述同一问题:
- 以最具体的为基础（优先用有行号的 edge case JSON）
- 将其他发现的独特细节合并到 `detail` 字段
- `source` 标记为合并来源

#### 3.3 分类（每个发现归入且仅归入一类）

| 分类 | 含义 | 条件 |
|---|---|---|
| **intent_gap** | Spec/意图不完整，无法从现有信息解决 | 仅 review_mode="full" |
| **bad_spec** | Spec 本身有问题或模糊 | 仅 review_mode="full" |
| **patch** | 代码问题，可简单修复 | 所有模式 |
| **defer** | 预存问题，不是当前变更引入的 | 所有模式 |
| **reject** | 噪声、误报或已在别处处理 | 所有模式 |

如果 review_mode="no-spec"，原本应为 intent_gap/bad_spec 的发现 → 重分类为 patch 或 defer。

#### 3.4 丢弃所有 reject，记录 reject 数量

---

### Step 4: 呈现报告

按类别分组呈现（仅展示有发现的类别）：

**Intent Gaps** — "这些发现表明捕获的意图不完整。建议在继续前澄清意图。"
- 列出每项 + 细节

**Bad Spec** — "这些发现表明 spec 应该修改。"
- 列出每项 + 细节 + 建议的 spec 修改

**Patch** — "这些是可修复的代码问题:"
- 列出每项 + 细节 + 位置

**Defer** — "预存问题（非当前变更引入）:"
- 列出每项 + 细节

**摘要**: X intent_gap, Y bad_spec, Z patch, W defer。R 个发现被拒绝为噪声。

**下一步建议**:
- 有 patch → "可在后续实现中处理，或手动修复"
- 有 intent_gap/bad_spec → "建议运行规划工作流澄清意图或修改 spec"
- 仅有 defer → "当前变更无需行动，延迟项已记录供未来关注"

**⚠️ 重要**: 不自动修复任何内容。呈现发现，让用户决定下一步。

---

## Nexus 工具映射

| 操作 | 工具 |
|---|---|
| 获取 git diff | `system_run` |
| 读取 spec/story 文件 | `read_vault` |
| 并行审查 subagent | `dispatch_subagent` × 3 |
| 读取项目代码（Edge Case Hunter）| `code_read_file` |
| 写入审查报告 | `write_vault` |
