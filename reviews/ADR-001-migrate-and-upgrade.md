# ADR-001: 从 analysis/wiki 迁移到桌面 knowledge-vault 并升级治理规则

- Date: 2026-07-21
- Status: Accepted

## Context

个人本地知识库原位于 `~/workspace/analysis/wiki/`，是 Karpathy LLM Wiki 模式的最初实现，只有 `schema.md`、`sources/`、`pages/` 三块，缺少：

1. 双向溯源机制（raw ↔ pages）
2. Frontmatter 强制规范
3. Lint 能力
4. 决策记录（ADR）
5. 独立的位置，方便迁移到其他机器或其他 AI 读取

同时用户提供了公司「某公司」vault 规范作为参考。

## Decision

1. 将 vault 整目录 `mv` 到 `/path/to/knowledge-vault/`
2. 目录 `sources/` 重命名为 `raw/`（对齐主流约定）
3. 新增顶层文件：`AGENTS.md`（治理契约）、`lint.py`（体检脚本）
4. 新增目录：`reviews/`（ADR）、`templates/`（模板）
5. 强制 raw frontmatter 字段：`date/source/type/tags/wiki_refs/status`
6. 强制 pages 必须有 `## 来源` 区块并回链 raw
7. Lint 三级风险（🟢 自动修 / 🟡 只报告 / 🔴 走 ADR），默认 dry-run
8. 引入 ADR 机制记录未来的规则变更

## Consequences

**好处：**
- 迁移友好：所有引用相对路径，整目录复制即可
- 其他 AI 接手时读 `AGENTS.md` 即可上手
- 孤儿 raw / 死链一键发现
- 决策有档案，未来重构不会失忆

**代价：**
- 摄入流程变复杂：raw frontmatter 强制填写、pages 强制回链、`wiki_refs` 回填
- 现存 raw 文件（`raw/articles/2026-07-20-karpathy-llm-wiki.md`）需要补齐 frontmatter（本次一并处理）

**后续：**
- OpenClaw workspace 里的 `AGENTS.md` 里对 `wiki/` 的引用需要更新为新路径
- 未来若涉及规则或目录结构重大调整，走 ADR-002...

## Alternatives Considered

- **在原位置就地升级**：❌ 用户明确希望迁移到桌面方便备份和其他 AI 读取
- **保留 `sources/` 命名**：❌ 与公司规范和 Karpathy 原文习惯不一致；重命名一次性成本可接受
- **完全照搬公司知识库规范（5 大分类 + AI 新闻时效字段 + Triage 衰减）**：❌ 个人 wiki 数据量小，一开始不需要这些重规则；先落地核心的双向溯源和 lint，其他等 ADR-002+ 按需引入
