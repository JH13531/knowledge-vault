# ADR-011: 去重闸门前移 + 人工保护区收敛为单章节

- Date: 2026-10-04
- Status: Accepted

## Context

2026-10-04 用户提出两个问题：

1. **「在浏览器/飞书里剪藏相同内容，前端都会报错，但文件依然入库了吗？」**
2. **「人工保护区应该只有 `## 我的思考` 一个栏目。」**

### 问题 1：去重是"事后通知"而非"事前闸门"

审查 `tools/clip-server.py` 后发现三条入库路径的去重时机全部有缺陷：

| 路径 | 现象 |
|---|---|
| `_smart_clip()`（后端直抓） | 第 1446 行先调 `clip_from_url()`，而后者第 1096 行**已经写盘**；第 1451 行才拿 `existing` 判重。文件已落地才报 409 |
| `handle_browser_result()`（浏览器协助） | **完全没有查重**，直接 `generate_card` → 写盘 → 入队 |
| `handle_clip()`（浏览器插件推内容） | 有查重且返回 409，但"新版更完整"分支是先删旧文件再继续写，与其余分支耦合在一段 60 行的嵌套 if 里 |

更糟的是 `_smart_clip()` 原第 1500 行 `elif` 与第 1517 行 `if` 条件完全相同（`new_wc > old_wc * 1.2`）——第 1517 行是**死代码**，永远不会执行；而 live-source 的实际分支（第 1562 行 `else`）与它自己的注释"live-source → 检查是否需要新版本"自相矛盾。

**结论：用户感知的"前端报错"是真的，但报错的时刻太晚——文件早已入库。** 这正是 ADR-010 里 7 份重复 Codex raw 的产生机制。

### 问题 2：保护区范围定义过宽

§5.5.1 原文：

> `## 我的思考` **及之后的所有内容**不得修改或删除（包括 `## 来源` 章节）

这条规则把 `## 来源` 也圈进了不可修改区，直接冲突 §5.4：

> **回填 raw：** 提炼完 wiki 后，必须回到对应 raw 文件，把该 wiki 页路径追加进 `wiki_refs` 数组。

`tools/wiki_gen.py` 的 `_merge_user_region()` 因此被迫写成"整段冻结"——第 334 行 `old_content[protected.start():]` 会把 `## 来源` 一并带走，第 337 行只能"在冻结区里补链接"，AI 无法正常维护来源列表。归档场景下（ADR-010）wiki 来源列表必然失同步，只能人工修——`wiki/queries/2026-10-04-clip-dedup-rootcause.md` 已记录这一未解张力。

用户指出正确做法：**保护区只应是 `## 我的思考` 这一个章节。**

## Decision

### 1. 统一写盘前去重闸门

新增 `_preflight_duplicate(normalized_url, new_content, force) -> dict | None`，作为**所有入库路径唯一的判定入口**，语义明确：

- `None` → 无重复，正常落盘
- `{"_status": 409, ...}` → 重复，调用方直接返回，**绝不落盘**
- `{"_overwrite": True, ...}` → 新版更完整（且非 live-source），删旧后落盘
- `{"_version": N, ...}` → live-source 有实质增长，创建新版本链

`clip_from_url()` 增加 `dry_run=True`：只抓取 + 生成卡片，不写盘、不下图、不入队，供 `_smart_clip()` 先判定再决定是否真正落盘。

改造后的路径：

| 路径 | 现在 |
|---|---|
| `_smart_clip()` | 先 `clip_from_url(dry_run=True)` → `_preflight_duplicate` → 判定通过才真正写盘 |
| `handle_browser_result()` | 补上去重闸门，重复则 `complete_task` 标记 duplicate 并返回 409 |
| `handle_clip()` | 改用同一个闸门，删掉重复逻辑 |

顺带清除了死代码与自相矛盾的注释。

### 2. 保护区收敛为单章节

§5.5.1 改为：**只保护 `## 我的思考` 章节本身**（从该标题到下一个同级 `##` 标题为止）；`## 来源` 属普通区，AI 需正常维护。

`wiki_gen._merge_user_region()` 重写：

- 精确切出 `## 我的思考` 到下一个 `##` 之间的区间作为保护区
- 新内容的占位思考段被替换，不产生重复章节
- 新增 `_extract_source_links()` / `_ensure_source_links()`：重写时**累积**旧来源回链，不因 AI 重写而丢失

### 3. 新增 lint 检查 25 + 存量修复

检查 25「人工保护区规范」：

- wiki 页（index/log/queries 除外）缺失 `## 我的思考` → 🟢（`--fix` 插入空占位）
- 出现多个 `## 我的思考`，或它位于 `## 来源` 之后 → 🟡

执行 `lint.py --fix` 为 18 个历史 wiki 页补上保护区，lint 低风险项归零。

## Consequences

**正面：**

- 「前端报错即已入库」的现象消除——重复内容现在在写盘前被拦下
- 浏览器协助路径（此前完全裸奔）纳入统一闸门
- AI 恢复维护 `## 来源` 的能力，归档导致来源失同步的张力解开
- 来源回链在页面重写时累积而非重置

**代价 / 注意：**

- `_smart_clip()` 会多一次抓取（dry-run 一次 + 正式落盘一次）。当前实现是 dry-run 后重新 `generate_card` 写盘，未复用 dry-run 的中间结果——抓取本身只发生一次（`clip_from_url` 内部只在 dry-run 分支提前返回），但 `generate_card` 会跑两次。可接受；若日后性能敏感可把 dry-run 的 `content` 直接复用。
- 存量 5 份重复 Codex raw 与 29 张非 WebP 图片仍未处理，属独立议题（ADR-010 已记录前者）。

## 相关

- [[reviews/ADR-010-archive-duplicate-codex-raw.md]] — 本 ADR 修复的重复入库，正是 ADR-010 处理的存量
- [[wiki/queries/2026-10-04-clip-dedup-rootcause.md]] — 去重失效根因分析，本 ADR 为其 P0 修复落地
