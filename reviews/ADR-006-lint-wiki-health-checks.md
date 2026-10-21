# ADR-006: Lint 新增 wiki 健康检查（4 项）

- Date: 2026-07-21
- Status: Accepted

## Context

v2.1 修正了 raw 严重违反后，对照 Karpathy 原文盘点还剩 4 处 lint 层面的违反：

1. **Lint 不检查矛盾** — Karpathy 要求"noting where new data contradicts old claims"
2. **Lint 不检查陈旧声明** — Karpathy 要求"stale claims that newer sources have superseded"
3. **Lint 不检查缺失交叉引用** — Karpathy 要求"missing cross-references"
4. **Lint 不检查缺失概念页** — Karpathy 要求"important concepts mentioned but lacking their own page"

其中第 4 项（缺失概念页）难以用规则检测，需要 LLM 语义判断，留待后续月度 LLM 审查。前 3 项都可以用 Python 规则检测，本次一并实现。

同时 wiki 页面缺少支撑这些检查的 frontmatter 字段（`last_verified`、`contradicts`），需要新增。

## Decision

### 新增 wiki frontmatter 字段

```yaml
last_verified: YYYY-MM-DD   # 内容最后被验证为正确的日期
contradicts: []             # 与本页面有事实冲突的其他页面路径
```

- `last_verified`：每次 ingest 涉及本页时刷新
- `contradicts`：显式标记矛盾关系，lint 校验双向性

### 新增 4 项 lint 检查

| # | 检查 | 风险级 | 检测原理 |
|---|------|--------|---------|
| 13 | `last_verified` 日期校验 | 🟡 中 | >6 个月 info 提示；>12 个月 🟡 告警；格式/未来日期 🟢 低 |
| 14 | 同标签更晚 raw 未引用 | 🟡 中 | 标签重叠的 raw，published_at > wiki 的 last_verified，且不在 wiki sources 中 |
| 15 | 同标签 wiki 未互相引用 | 🟡 中 | 共享 ≥1 标签的 topics 页，任一方未引用对方 |
| 16 | `contradicts` 双向性校验 | 🟡 中 | A.contradicts 含 B 时，B.contradicts 必须含 A；死链 🟢 低 |

### 未纳入本次的检查

- **缺失概念页检测**：需要 LLM 语义判断，留作月度 LLM 审查
- **数据缺口检测**：TODO/TBD 标记扫描有价值但非 Karpathy 核心要求，留待后续

## Consequences

**好处：**

- Karpathy 列出的 6 项 lint 检查现在覆盖了 5 项（只剩"缺失概念页"需 LLM）
- 引入 `last_verified` 后，陈旧声明有明确的时间锚点
- 引入 `contradicts` 后，矛盾声明有显式的双向追踪
- 同标签检查推动 wiki 页面之间形成真正的知识网络

**代价：**

- wiki 模板更复杂（多了 2 个字段）
- 摄入流程多一步（刷新 last_verified）
- 同标签检查可能产生噪音（标签相似但主题无关的页面）

**缓解措施：**

- 标签噪音：用户可调整标签粒度，或忽略 🟡 中风险报告
- last_verified 维护成本：作为摄入流程的标准步骤，不额外增加心智负担

## Alternatives Considered

**只加 last_verified，不加 contradicts：** ❌ contradicts 是 Karpathy 明确要求的矛盾检测基础设施，没有它矛盾检测无法落地

**全部 6 项都实现：** ❌ "缺失概念页"和"数据缺口"需要 LLM 语义判断，无法用规则检测；强行实现会产出大量误报

**用 embedding 做语义相似度检查：** ❌ 过度设计，需要引入额外依赖（向量数据库 / embedding 模型），当前规模不需要
