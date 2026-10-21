---
adr_id: 007
title: AI Coding 主题拆分 + 6 篇 TRAE raw 提炼
date: 2026-07-29
status: accepted
---

# ADR-007: AI Coding 主题拆分 + 6 篇 TRAE raw 提炼

## 背景

知识库 wiki/topics/ 中 AI 相关文件持续增长：
- 原有 AI topic 8 个（agentic-coding-principles, function-calling, harness-engineering, llm-wiki, skills-guide, token-context-window, vibe-coding-guide, feishu-agent-group-chat）
- 新增 6 篇 TRAE raw（技巧: Skill/Rules/Token/模型选择）待提炼

按 AGENTS.md §5.1 拆分规则：「当 wiki/topics/ 下同一领域的文件 ≥ 5 个时，创建子文件夹」。AI Coding 领域 8 个文件已触发拆分条件。

## 决策

### 1. 创建 `wiki/topics/ai-coding/` 子目录

把 8 个 AI 相关 topic 迁移过去：
- agentic-coding-principles.md
- function-calling.md
- harness-engineering.md
- llm-wiki.md
- skills-guide.md
- token-context-window.md
- vibe-coding-guide.md
- feishu-agent-group-chat.md

**命名选择**：用 `ai-coding/` 而非 `ai/`，更精确反映内容聚焦于 AI 编程/工程，避免和"通用 AI 概念"混淆。

### 2. 提炼 6 篇新 TRAE raw

| raw 文件 | 提炼去向 |
|---------|---------|
| 技巧-如何写好一个-Skill.md | 更新 `skills-guide.md`（5 设计标准、自由度分级、评测驱动迭代） |
| 技巧-在TRAE中快速上手Skills.md | 更新 `skills-guide.md`（TRAE 定位与创建流程） |
| 技巧-10个Token节省技巧.md | 更新 `token-context-window.md`（新增"10 技巧"节） |
| 技巧-Rules使用指南.md | 新建 `rules-guide.md`（多规则管理 + 4 种生效方式 + L0-L3 拆分） |
| 模型选择指南-TRAE中国版.md | 新建 `model-selection.md`（合并中外两版） |
| 模型选择指南-TRAE国际版.md | 新建 `model-selection.md`（同上） |

### 3. 更新 wiki/index.md

按「非 AI 主题 / AI Coding」两分区重组，路径更新为 `wiki/topics/ai-coding/xxx.md`。

## 后果

**正面：**
- AI 主题聚合，新读者一目了然
- 6 篇新 raw 提炼为 4 个 topic（2 更新 + 2 新建），知识密度提升
- 符合 §5.1 规则，为未来扩展留空间

**负面：**
- 所有引用旧路径的 wiki 内部链接需更新（本次一并处理）
- 子目录增加一层路径深度

**中性：**
- 非 AI topic 5 个（enterprise-vault, kb-intro, info-debt, feishu-cli, ssh-key），暂无需拆分

## 后续

- 新 raw 加入时，按 §4.5 判断是否需要 `fact_type: live-source`
- AI topic 数量若继续增长，考虑在 `ai-coding/` 内再按子领域拆分（如 `ai-coding/skills/`、`ai-coding/models/`）
