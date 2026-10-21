# ADR-002: pages/ 重命名为 wiki/，删除顶层 assets/

- Date: 2026-07-21
- Status: Accepted
- Supersedes: 部分 ADR-001 的目录布局

## Context

ADR-001 落地后，用户指出两个不一致点：

1. **命名不统一**：Karpathy 原文和「LLM Wiki」这个名字都用 `wiki/` 指代知识层；ADR-001 沿用了旧的 `pages/`，但同时把整个 vault 叫做「knowledge-vault」/「wiki」，语义混乱。
2. **顶层 `assets/` 冗余**：新规则明确图片跟 raw 走同名子目录（§8），不再需要顶层 `assets/`，`raw/assets/` 也是历史残留（当时没落地图片规范就先建了目录）。

两个目录当前都是空的，改动零成本。

## Decision

1. `pages/` → `wiki/`（目录 `mv`）
2. 删除顶层 `assets/` 和 `raw/assets/`
3. 全部文档同步更新引用路径

## Consequences

**好处：**
- 三层架构命名对齐：`raw/`（证据）+ `wiki/`（知识）+ `AGENTS.md`/`reviews/`（治理）
- 与 Karpathy 原文用词一致，其他 AI 读起来更顺
- 消除「vault 又叫 wiki 又有 pages」的心智负担

**代价：**
- 文档里所有 `pages/xxx` 路径需刷新（本次一并处理）
- ADR-001 里提到 `pages/` 的段落成为历史记录，不再回改（ADR 不修改，靠 Supersedes 关系体现）

**后续：**
- 未来讨论中「wiki 页面」= `wiki/*.md`；「本地 wiki」/「vault」= 整个 `knowledge-vault/` 目录
- `MEMORY.md` 和 `analysis/AGENTS.md` 里的路径引用同步更新

## Alternatives Considered

- **保留 `pages/`，仅去掉顶层 `assets/`**：❌ 不能解决语义冲突
- **把顶层 vault 目录改名，保留 `pages/`**：❌ 用户已选择「knowledge-vault」作为顶层名，改内部目录成本更低
