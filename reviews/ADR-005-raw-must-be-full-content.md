# ADR-005: Raw 必须为完整内容快照

- Date: 2026-07-21
- Status: Accepted

## Context

v1.0 迁移时，AI 自作主张把 raw 设计成"来源卡片"模式——每个 raw 文件只有摘要 + 外链，没有完整原文。这个设计在后续 v1.1 ~ v2.0 的所有迭代中都没有被质疑，直到用户发现：

> "wiki 比 raw 写的都更加完整，那 raw 的作用是什么？"

对照 Karpathy 原文检查：

> "Raw sources — your curated collection of source documents. Articles, papers, images, data files. These are immutable — the LLM reads from them but never modifies them. **This is your source of truth.**"

以及：

> "Obsidian Web Clipper is a browser extension that converts web articles to markdown. Very useful for quickly getting sources into your raw collection."

两条都明确指向"raw = 完整原文"。"来源卡片"模式违背了 Karpathy 的核心设计原则——raw 是 source of truth，不是书签。

**后果：**

- wiki 的内容实际是我重新从飞书 API / Gist 抓原文后写的，绕过了 raw
- 信任链断裂：wiki 不依赖 raw，依赖外部源
- 外部链接失效时，vault 没有证据保全能力

## Decision

1. **新增 §4.3「Raw 必须为完整内容快照（铁律）」**：明确 raw 必须包含完整原文，禁止来源卡片、节选、外链-only 模式
2. **重写现有 3 份 raw 文件**：从来源卡片升级为完整内容快照（一次性修正窗口）
3. **原 §4.3 Raw 不可修改 → §4.4**：保留冻结规则，新增"一次性修正窗口"例外
4. **更新摄入流程**：第一步是抓取完整原文，不是写来源卡片

## Consequences

**好处：**

- 信任链完整：raw 真的是 source of truth，wiki 从 raw 提炼
- 证据保全：外部链接失效、原文被改，vault 仍有原始快照
- 离线可用：不需要网络访问原文
- 与 Karpathy 设计、公司标准完全对齐

**代价：**

- Raw 文件变大（每份从 ~35 行 → 150~400 行）
- Git 同步时占用更多空间（但 markdown 文本依然很轻）
- 摄入流程第一步变重（要抓完整内容，不是只记个链接）

**一次性修正窗口：**

本次重写是历史遗留问题的集中修正。根据新规则 §4.4，一旦被 wiki 正式引用或过了 ingest 流程，raw 即永久冻结。未来的修正只能通过新建 raw 文件完成。

## Alternatives Considered

**保留来源卡片模式：** ❌ 违背 Karpathy 原则，信任链断裂

**混合模式（摘要 + 完整内容分段）：** ❌ 增加复杂度，且"摘要段"和"完整内容段"会重复

**只重写新 raw，保留旧 raw：** ❌ 现有 3 份 raw 都是问题源头，不修正会污染后续的 wiki 引用
