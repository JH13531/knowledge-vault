# ADR-003: wiki 目录合并 + index/log 移入 wiki/

- Date: 2026-07-21
- Status: Accepted

## Context

v1.1 的 wiki 目录按产出类型拆分：`concepts/`（概念）、`entities/`（实体）、`summaries/`（摘要）、`queries/`（分析产物）。随着内容积累，一个主题的知识被拆散到多个目录，增加检索成本。

同时 `index.md` 和 `log.md` 放在 vault 根目录，但它们导航/记录的是知识层（wiki）内容，不是 raw 层。根目录应该只留治理文件。

## Decision

对齐公司知识库规范思路：

1. **目录合并**：`wiki/concepts/` + `wiki/entities/` + `wiki/summaries/` 合并为 `wiki/topics/`。一份页面身兼摘要 + 概念 + 实体，按主题命名（如 `topics/llm-wiki.md`），不再按产出类型拆目录。
2. **index/log 移位**：`index.md` 和 `log.md` 从 vault 根移入 `wiki/` 下（`wiki/index.md`、`wiki/log.md`）。
3. **queries/ 保留**：问答产物的生命周期与主题页不同（有些是一次性的，有些晋升为 topics），单独保留。

## Consequences

**好处：**
- 一个主题的所有知识集中在 `wiki/topics/` 一个目录，检索成本降低
- 页面数减少（原来 concepts + summaries 各一份，现在合并成一份）
- index/log 归位知识层，根目录职责更清晰（只留治理文件）
- 对齐公司标准，未来跨库迁移更容易

**代价：**
- 历史路径全部失效（`wiki/concepts/xxx` → `wiki/topics/xxx`），需要批量更新引用
- 失去"按类型浏览"的能力（不能再"只看所有摘要"），但实际使用频率很低

**后续需要注意：**
- `queries/` 中被多次引用或用户明确有价值的分析，应晋升合并到 `topics/`
- 合并后的页面需要同时包含原 summary 的要点和原 concept 的体系化内容

## Alternatives Considered

**保留现状，只移 index/log：** 改动最小，但 wiki 目录拆散问题会随内容积累越来越严重。

**完全照搬公司 5 大类目录（01~05）：** 对公司多人多项目场景合适，对个人库过度设计——当前内容量远不到需要物理目录分 5 类的程度。

**按主题域分（ai/、tools/、...）而不是按类型分：** 比现状好，但当前只有 1 个主题页，过早分类反而增加维护成本。先用扁平 `topics/`，等内容超过 15-20 个再按域拆。
