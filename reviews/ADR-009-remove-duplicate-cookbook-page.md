# ADR-009: 删除重复的 codex-agent-cookbook 主题页

- Date: 2026-10-03
- Status: Accepted

## Context

ADR-007 执行了 `wiki/topics/` 的领域拆分，把 AI 编码相关主题页迁移到 `wiki/topics/ai-coding/` 子目录。但迁移**没有收尾**：根目录仍保留迁移前的旧副本 `wiki/topics/codex-agent-cookbook.md`，与新地址 `wiki/topics/ai-coding/codex-agent-cookbook.md` 并存。

现状对比（2026-10-03 实测）：

| 文件 | 字节 | 章节数 | 唯一内容 |
|---|---|---|---|
| `wiki/topics/codex-agent-cookbook.md`（旧） | 17,147 | 30 | 五层架构、五步测试法、最小可用目录结构 |
| `wiki/topics/ai-coding/codex-agent-cookbook.md`（新） | 19,136（迁移后） | 40+ | 定义/背景、初始化五件套、八步验收流程、版本化提交清单 |

新副本是旧副本的**超集**：

- 旧副本的「核心思想：五层架构」→ 新副本的「定义 / 背景 + 初始化五件套」已覆盖同样的五层定位
- 旧副本的「五个易混淆概念辨析」→ 新副本的「五个容易混淆的概念」是同一张表的扩展版（加了"典型适用场景"列 + 判断口诀）
- 旧副本的「五步测试法」→ 新副本的「八步验收流程」是同一套验收逻辑的扩展版（八步 = 静态检查 / 正例 / 反例 / 缺失信息 / 多 Agent 编排 + 三步扩展，评分标准一致）
- 旧副本的「常见故障与定位顺序」→ 新副本的「常见故障与定位 + 故障定位映射表」覆盖更全
- 旧副本「最小可用目录结构」的目录树→ 新副本原文中无完整版本，本文 Decision 中已迁移过去

两份页面都符合 lint 的全部格式检查，所以 lint 此前没有抓出重复（重复主题检测是 2026-10-03 本次补的检查 21）。

影响面（全仓库扫描）：

- 指向旧副本的引用共 3 条，全部来自 raw 的 `wiki_refs` 字段：
  - `raw/articles/2026-09-03-Codex-Agent-Cookbook：按需求创建、初始化与验收.md`
  - `raw/articles/2026-09-03-Codex-Agent-Cookbook：按需求创建、初始化与验收-1.md`
  - `raw/articles/2026-09-03-Codex-Agent-Cookbook：按需求创建、初始化与验收-3.md`
- 指向新副本的引用：`wiki/index.md`（索引）、`wiki/log.md`、4 份 raw 的 `wiki_refs`、`wiki/queries/2026-10-03-vault-review.md`

## Decision

1. **保留 `wiki/topics/ai-coding/codex-agent-cookbook.md`** 作为唯一权威版本（内容更全、已被 index 索引、被 4 份 raw 引用）。
2. **删除 `wiki/topics/codex-agent-cookbook.md`** 旧副本。其 `## 我的思考` 保护区为空（仅有注释占位），无人工内容需要迁移。
3. **迁移唯一内容**：旧副本独有的「最小可用目录结构」目录树，插入到新副本 `## 编写第一个自定义 Agent` 下的 `### 最小 TOML 模板` 之前。
4. **重连 3 条 `wiki_refs`**：把指向旧副本的 3 份 raw 的 `wiki_refs` 改为 `["wiki/topics/ai-coding/codex-agent-cookbook.md"]`（第 3 份 `-3` 随后按 ADR-010 归档，其引用随之流转到 `_archive/`，不再参与 lint）。
5. `wiki/index.md` 无需改动——它从头就指向新副本。

不修改新副本的 `## 来源` 章节：按 §5.4.1 人工编辑保护，AI 不得改动 `## 我的思考` 及之后的任何内容。

## Consequences

**好处：**

- 消除重复主题页，lint 检查 21（重复主题检测）恢复为 0 命中
- 知识收敛到一处，后续维护只需改一份
- `wiki/topics/` 根目录与 `ai-coding/` 子目录的关系恢复一致（与 ADR-007 的设计意图对齐）

**代价 / 需要注意：**

- 旧副本正文删除后，历史 `wiki/log.md` 中指向旧路径的记录**保留不动**——日志是演化史，不改。
- 任何外部粘贴的旧路径链接会 404。当前仓库内无此类链接（已全仓库扫描确认）。
- 归档后的 `-3` raw 不再是孤儿（`wiki_refs` 仍指向有效页面），仅移入 `_archive/` 退出 lint 扫描，**不删除**，保留证据（§4.4 不可变原则）。

## Alternatives Considered

**方案 A：反向迁移（保留根目录、删除 `ai-coding/` 副本）—— 不采用。**
根目录版本内容更少（17.1K vs 19.1K），且 `wiki/index.md` 与 4 份 raw 都已指向 `ai-coding/`。反向迁移需要改动 6 处引用而非 3 处，收益为负。

**方案 B：把两份合并成一份，保留两份路径互为 `[[ ]]` 引用 —— 不采用。**
`AGENTS.md` §5.3.1 的交叉引用标准要求"阅读路径价值"，重复主题之间不构成阅读路径；且两个同内容页面会让 lint 的标签重叠检查（检查 15）持续误报。

**方案 C：先给旧副本打 `status` 标记而非删除 —— 不采用。**
wiki 层没有 `status` 字段；加字段等于改契约。lint 检查 21 的语义就是"同 stem 重复即告警"，标记不解决告警。

**方案 D：仅标记 deprecated 后留在原位 —— 不采用。**
同 C，且会让 `wiki/topics/` 根目录长期残留非主题文件。
