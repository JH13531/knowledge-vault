# ADR-004: 合并 schema.md 到 AGENTS.md + queries 清理机制

- Date: 2026-07-21
- Status: Accepted

## Context

v1.4 之前，vault 有两份顶层治理文件：
- `AGENTS.md`：规则（怎么做）
- `schema.md`：格式（长什么样）

两份文件存在大量重叠：目录结构 tree、frontmatter 字段列表、wiki 规范、模板列表都各写了一遍。每次变更 frontmatter 或目录结构都要两边同步更新，维护成本高且容易不一致。

公司标准也是两份（`AGENTS.md` + `kb/rules.md`），但公司有合理性：`kb/rules.md` 是董事会通过的架构权威，`AGENTS.md` 是 agent 的操作手册，权威来源不同。个人库没有这个区别。

同时 `wiki/queries/` 自 v1.2 引入以来一直没有清理规则，长期可能积累无用产物。

## Decision

1. **合并 `schema.md` 到 `AGENTS.md`**：规则 + 格式合为一份。`AGENTS.md` 成为唯一的治理契约。
2. **删除 `schema.md`**：不再保留，避免重复。
3. **新增 queries 清理机制**：每月检查 `wiki/queries/`，按引用数和时效决定晋升/删除。

## Consequences

**好处：**
- AI 接手只需读一个文件，阅读顺序从 4 步简化为 3 步
- 消除两份文件的重复内容和同步维护成本
- 规则变更只需改一处，不会再出现两份文件不一致的情况
- queries 有了生命周期管理，避免沦为半知识垃圾场

**代价：**
- `AGENTS.md` 变长（约 300 行），但仍在可控范围
- 失去"规则 vs 格式"的概念拆分——对个人库来说这个拆分意义不大

**后续需要注意：**
- 如果未来 vault 规模大到需要"架构权威"和"操作手册"分离（比如多人贡献），可以再拆
- queries 清理需要定期执行，建议作为月度 lint 的一部分

## Alternatives Considered

**保留现状两份文件：** 维护成本高，每次 frontmatter 或目录变更都要改两处，容易不一致。

**把 AGENTS.md 内容迁到 schema.md：** 反过来不合理——`AGENTS.md` 是业界通用的治理文件名，其他 AI 更容易识别。

**保留 schema.md 作为"极简速查卡"：** 增加复杂度但价值有限——需要速查时直接搜 AGENTS.md 的标题即可。

**不做 queries 清理：** 长期积累会污染知识层，违背"知识持续复利"的初衷。
