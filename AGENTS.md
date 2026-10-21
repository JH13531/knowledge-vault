# AGENTS.md — 个人知识库治理契约

> 版本：v2.11
> 最后更新：2026-10-04
> 位置：本目录（`knowledge-vault/`）
> 参考：Karpathy LLM Wiki 模式 + 公司知识库 Vault 规范（简化个人版）

---

## 0. 阅读顺序（任何 AI 接手前先读）

1. 本文件 `AGENTS.md` —— 唯一的治理契约，涵盖规则 + 格式
2. `wiki/index.md` —— 当前 vault 有什么内容
3. `wiki/log.md` —— 最近发生了什么

---

## 1. 三层架构（Karpathy Wiki）

| 层 | 目录 | 语义 | 读写规则 |
|---|---|---|---|
| **证据层** | `raw/` | 原始素材（文章/对话/截图），冻结 | AI 只读；写入即冻结 |
| **知识层** | `wiki/` | AI 提炼的结构化知识 | AI 可写、可迭代 |
| **治理层** | `AGENTS.md` / `reviews/` | 规则和决策 | 规则变更走 ADR |

**铁律：**
- 所有输入 → `raw/`，所有输出 → `wiki/`
- **永远不跳过 raw 直接写 wiki**（跳过 raw = 跳过思考）
- raw 存原始上下文（谁说、何时说、什么场景），wiki 存提炼后的知识

---

## 2. 目录结构

```
knowledge-vault/
├── AGENTS.md              # 治理契约（本文件，规则 + 格式）
├── lint.py                # 体检脚本（默认 dry-run）
├── raw/                   # 证据层（只读）
│   ├── articles/          # 文章、网页剪藏
│   ├── notes/             # 笔记、对话记录
│   └── _archive/          # stale 归档（可选，自动生成）
├── wiki/                  # 知识层（AI 维护）
│   ├── index.md           # 知识层导航索引
│   ├── log.md             # 知识演化日志
│   ├── topics/            # 主题知识页（摘要 + 概念 + 实体合一）
│   └── queries/           # 问答与分析产物
├── reviews/               # ADR 决策记录
└── templates/             # 各类文件模板
    ├── raw.md             # 原始资料模板
    ├── page.md            # 主题知识页模板
    ├── query.md           # 查询产物模板
    └── adr.md             # ADR 模板
```

**图片放哪：** 跟着 raw 文件走，放到同名子目录下（不设顶层 `assets/`）。详见 §7。

---

## 3. 提炼频率

| 触发 | 场景 | 频率 |
|---|---|---|
| **事件驱动（主）** | 用户明确摄入新资料 / 完成一次有价值的分析 / 得到一个值得留存的结论 | 立即处理 |
| **周度轻量 lint（辅）** | 扫描孤儿 raw、死链、格式问题 | 每周一次 |
| ❌ 不推荐 | 每天批量提炼 | 无效摄入 + 冲突风险 |

---

## 4. Raw 层规则

### 4.1 任何原始素材先无脑进 raw

不要纠结「值不值得」——先冻结，再决定要不要提炼。

### 4.2 Frontmatter 强制规范

每个 raw 文件顶部必须包含（YAML frontmatter）：

```yaml
---
date: YYYY-MM-DD              # 摄入日期
source: "对话:场景/日期" | "本地文件:路径"   # 仅非 URL 来源时填写
source_url: <URL>             # 原始链接（URL 来源时必填）
published_at: YYYY-MM-DD      # 原发布日期（缺失时 lint 可从文件名自动提取）
type: article | screenshot | audio | chat | doc | note | selection | video
tags: [主题标签]
wiki_refs: []                 # ← 数组：指向 wiki/ 下引用了本 raw 的页面路径
status: classified | triage | anomaly
---
```

**关键字段说明：**

- **`source_url`**：当 `source` 是网页链接时必填，便于日后验证链接可达性；`source` 为对话/本地文件时可留空
- **`published_at`**：原文发布日期（区别于 `date` 摄入日期）。缺失时 lint 会从文件名 `YYYY-MM-DD-xxx.md` 自动提取
- **`wiki_refs`**：双向溯源的核心
  - 空数组 `[]` = 未提炼的孤儿 raw，lint 一秒识别
  - 提炼一页 wiki 后，把该 page 路径追加进来，例如：`["wiki/topics/xxx.md"]`
- **`status`**：
  - `classified`（默认）：分类清晰
  - `triage`：拿不准放哪，但**必须放进现有分类**（不飘在 raw/ 根目录）
  - `anomaly`：格式或内容异常，挂起待处理

#### 4.2.1 剪藏工具自动写入的字段（可选）

`tools/clip-server.py` 在入库网页 / 视频 / 文档时，会额外写入以下元数据字段。它们是**可选**的，不参与必填校验，但 lint 已将其纳入 `VALID_RAW_FIELDS`，不会被判为「非法字段」：

- **`source_id`**：平台内唯一标识，如 `"bilibili:BV1yq..."` / `"youtube:VIDEO_ID"` / `"wechat:xxx"`，用于去重与跨平台溯源
- **`author`**：原作者 / 发布者昵称
- **`word_count`**：正文估算字数（视频类为字幕 / 文稿字数）
- **`duration`**：音视频时长，格式 `MM:SS` 或 `HH:MM:SS`（仅视频 / 音频类有意义）
- **`type: video`**：新增的媒体类型，覆盖 B 站 / YouTube 等视频源；早期预留的 `selection` 类型一并保留

> 注意：以上字段由工具自动生成，人工手写的 raw 不需要补这些字段。

### 4.3 Raw 必须为完整内容快照（铁律）

**Raw 必须包含原始资料的完整内容，不允许只放链接或摘要卡片。**

- Karpathy 原文："Raw sources — your curated collection of source documents. These are immutable. This is your **source of truth**."
- Web Clipper、lark-cli fetch、PDF 提取等工具的作用就是把**整篇内容**转成 markdown 存入 raw
- Raw 的价值在于**证据保全**：外部链接失效、原文被改、离线查阅——都靠 raw 兜底
- 如果 raw 只是书签，wiki 实际依赖的是外部源而非 raw，信任链断裂

**正确格式：**

```markdown
---
frontmatter
---

# 标题

> 来源说明（URL / 文件路径 / 对话记录场景）

## 原文快照

[完整的原始内容，verbatim]

> 注：本页是不可变来源快照。完整原文以 source_url 为准。
```

**禁止的格式：**

- ❌ 来源卡片（只有摘要 + 外链）
- ❌ 节选 + "完整内容见链接"
- ❌ 只放一个 URL 没有正文

### 4.4 Raw 不可修改

Frontmatter 写完就冻结，正文一并冻结。这是证据层——信任来自不可变性。

**一次性修正窗口：** 如果 raw 在创建时格式错误（如本次 ADR-005 记录的"来源卡片"问题），可以在 ADR 记录下进行一次性重写。**一旦 raw 内容被正式确认（被 wiki 引用或过了 ingest 流程），即永久冻结。**

如果原始资料本身错了，新建一份 raw 备注「更正 xxx」，不修改旧的。

### 4.4.1 Stale 归档与 Triage 衰减

**Stale 归档：**
`status: stale` 或 90 天未被引用 → 移动到 `raw/_archive/`（保留但不进 lint 报告）。

**Triage 衰减（简化版）：**

| 阶段 | 时间 | 动作 |
|------|------|------|
| lint 高亮 | T+7 天 | lint 报告明确标记，提醒尽快处理 |
| 建议归档 | T+30 天 | lint 建议补充分类或移入 `raw/_archive/` |

lint 按 `date` 字段计算存活天数。`status: triage` 不允许长期悬挂——要么明确归类，要么归档。

### 4.5 外部易变源管理（GitHub / 飞书 / 网页）

> 适用于 `source_url` 指向外部、用户明确希望跟踪更新的文档。**默认所有 raw 都是快照，不参与更新跟踪**——只有用户摄入时明确说"跟踪更新"才启用本节。
> 内部产生的 raw（会议记录、对话摘录）或一次性存档文章（飞书副本、定稿文档）不适用本节。

#### 4.5.1 Frontmatter 扩展字段

```yaml
---
# 现有字段保留（date, source, source_url, ...）

# 新增（仅当用户明确标记跟踪更新时填写）：
fact_type: live-source                    # 标记为"需要跟踪更新的外部源"
platform: github | feishu | notion | web  # 平台类型
version_id: <commit-sha | edit-time | content-hash>  # 版本标识
tier: 1 | 2 | 3                           # 镜像深度（见 §4.5.2）
subdocs_count: N                          # 子文档数
last_checked: YYYY-MM-DD                  # 最近检查更新时间
---
```

#### 4.5.2 子文档镜像分级

按子文档数量 N 决定镜像深度：

| Tier | 条件 | 处理 | 存储 |
|------|------|------|------|
| 1 | N ≤ 30 | 完整镜像，目录结构镜像原平台 | `raw/articles/{date}-{slug}/` 文件夹 |
| 2 | 30 < N ≤ 100 | 主文件 + 一级子文档，深层链接改写为绝对 URL | 单文件 |
| 3 | N > 100 | 仅主文件，所有子文档链接改写为绝对 URL | 单文件 |

#### 4.5.3 子文档识别规则（按平台）

| 平台 | 内部子文档判断 | 版本标识来源 |
|------|--------------|--------------|
| GitHub | 同仓库相对路径 `.md` 链接 | `git -c http.version=HTTP/1.1 ls-remote URL HEAD` |
| 飞书 Wiki | 同 space_id 的 wiki 节点 | `lark-cli wiki get_node` → `edit_time` |
| Notion | 同 workspace 的 sub-pages | API `last_edited_time` |
| 网页 | 同域名 + 同路径前缀 | HTTP `ETag` 或 `Last-Modified` |

#### 4.5.4 更新检测与同步

**核心原则：源文档更新时，创建新版本 raw，旧版本保留（不可修改原则）。**

**默认行为：** 所有 raw 都是快照，不进入更新检测流程。仅 `fact_type: live-source` 的 raw 参与。

**Ingest 时询问（可选）：** 当摄入带 `source_url` 的外部源时，可问一句：
> "这份源文档未来可能更新，需要我标记为 live-source 跟踪更新吗？"
- 是 → 加 §4.5 字段
- 否 / 不答 → 普通 raw，不加

**更新工作流（针对 live-source）：**
1. 用户："检查有没有更新的源文档"
2. AI 扫描所有 `fact_type: live-source` 的 raw，对比平台当前 `version_id`
3. 列出 outdated 的源
4. 用户决定更新哪些
5. AI 创建新版本 raw（带 `-v2` / `-v3` 后缀）
6. wiki/topic 更新指向最新版，旧版保留作历史

**时效提醒（扩展 triage 衰减思路）：**
- `last_checked` 超过 90 天 → lint 报告静默列表
- `last_checked` 超过 180 天 → lint 建议重新检查或标记为 `stale`

---

## 5. Wiki 层规则

### 5.1 Wiki 目录结构

```
wiki/
├── index.md       # 知识层导航索引
├── log.md         # 知识演化日志
├── topics/        # 主题知识页（摘要 + 概念 + 实体合一）
│   └── <领域>/    # 同领域 ≥5 个文件时启用（ADR-007 起已启用：ai-coding/）
└── queries/       # 问答与分析产物
```

**设计思路（对齐公司知识库规范）：**
- **一份页面身兼摘要 + 概念 + 实体**，不再按产出类型拆目录
- 按主题命名文件（如 `topics/llm-wiki.md`），不按类型拆散
- `queries/` 单独保留：问答产物的生命周期与主题页不同（有些是一次性的，有些晋升为 topics）

**拆分触发条件：** 当 `wiki/topics/` 下同一领域的文件 ≥ 5 个时，为该领域创建子文件夹（如 `wiki/topics/金融/`），`git mv` 迁移，写 ADR 归档。

**现状（截至 v2.6）：**

| 层级 | 内容 |
|---|---|
| `wiki/topics/` 根目录 | 7 个跨领域的独立主题页（llm-wiki、feishu-cli-guide、ssh-key-authentication 等） |
| `wiki/topics/ai-coding/` | AI 编码领域（llm-wiki、codex-agent-cookbook 等），由 ADR-007 拆分产生 |

根目录之所以保持扁平：它装的是**跨领域独立主题**，与本领域的多个文件不同——不构成"同一领域 ≥5 个"的拆分条件。迁移时**旧路径副本必须一起删除**（见 ADR-009：只迁不删会产生重复主题页，且 lint 的重复主题检查要到 v2.5 才补上）。

### 5.2 Wiki 目录约定

| 目录 | 内容 | 文件命名 |
|---|---|---|
| `wiki/topics/` | 主题知识页（摘要 + 概念 + 实体合一） | `主题名.md`（英文/拼音，下划线分隔） |
| `wiki/queries/` | AI 生成的分析/对比/报告 | `YYYY-MM-DD-简短描述.md` |

### 5.3 Wiki 文件规范

- 使用 Markdown，用 `##` 作主要分区标题
- 引用其他页面：`[[wiki/topics/xxx.md]]`
- 引用 raw：`[[raw/xxx/yyy.md]]`
- **每个 wiki 页面必须以 `## 来源` 结尾并列出 raw 回链**

#### 5.3.1 交叉引用（`## 相关页面`）选择标准

**核心原则：阅读路径价值。** 问自己——"正在读本页的人，跳过去能否获得直接帮助？"

| 判断 | 标准 | 示例 |
|------|------|------|
| ✅ 引用 | 因果/互补/层级/实践载体关系 | Token→Agentic Coding（因果），FC↔Skills（互补辨析） |
| ❌ 不引用 | 仅共享标签但主题不同 | 群聊↔Skills（都含 Agent 标签但无关） |
| ❌ 不引用 | 抽象概念关联，无直接阅读路径 | HarnessSkills（Skills 是 Harness 的一种，但读 Skills 不需要先读 Harness） |

- 宁缺毋滥：一般 2-4 个为宜，不凑数
- 每个引用附一句话说明关联性（如 `— Token 限制是短对话策略的理论基础`）

### 5.4 Wiki Frontmatter 约定

每个 wiki 主题页除基础字段外，建议填写以下支持 lint 健康检查的字段：

```yaml
last_verified: YYYY-MM-DD   # 内容最后被验证为正确的日期
                            # lint 会按 6 个月 / 12 个月分级告警
                            # 每次 ingest 涉及本页时自动刷新
contradicts: []             # 与本页面有事实冲突的其他页面路径
                            # lint 校验双向性：若 A.contradicts 含 B，则 B.contradicts 必须含 A
```

**摄入流程**：每次 ingest 涉及某 wiki 页面时，顺手把该页的 `last_verified` 刷新为当天日期。

### 5.5 双向溯源

wiki 页面末尾必须有：

```markdown
## 来源
- [[raw/articles/2026-07-20-karpathy-llm-wiki.md]]
```

**回填 raw：** 提炼完 wiki 后，必须回到对应 raw 文件，把该 wiki 页路径追加进 `wiki_refs` 数组。

### 5.5.1 人工编辑保护（我的思考）

wiki 页面中 `## 我的思考` **章节本身**是**人工编辑保护区**，AI 在更新/重提炼 wiki 页时必须遵守：

- **只保护 `## 我的思考` 这一个章节**：从 `## 我的思考` 标题起，到下一个同级 `##` 标题（即 `## 来源`）之前为止
- 该章节内的内容 AI **不得修改、删除或重写**
- `## 我的思考` **之后**的章节（如 `## 来源`）**不属于**保护区，AI 可正常维护（§5.4 要求 AI 回链来源）
- AI 只能更新 `## 我的思考` 之前的内容（定义、核心内容、关键要点、相关页面）以及其后的 `## 来源`
- 如果页面没有 `## 我的思考`，AI 可根据模板在 `## 来源` 之前插入空的占位章节
- 用户在本章节记录个人感悟、实践经验、与其他知识的关联思考——这是知识从"别人的"变成"自己的"的关键区域

**为什么只保护一个章节：**
- 保护区越小，语义越清晰——AI 判断"能不能改"只需看是否落在该章节边界内
- `## 来源` 是结构化元数据，AGENTS.md §5.4 明确要求 AI 维护双向回链；若把它划进保护区，AI 就无法履行该义务
- 「保护一个章节」比「保护标题及其后全部内容」更不容易误伤

**判定边界（供 AI 与 lint 使用）：**
```
## 我的思考           ← 保护区起点
...                  ← 保护内容
## 来源               ← 保护区终点（此标题起为普通区）
```

### 5.5 页面模板

- 原始资料：`templates/raw.md`
- 主题知识页：`templates/page.md`
- 查询产物：`templates/query.md`
- ADR：`templates/adr.md`

### 5.6 queries 清理机制

定期（建议每月一次）检查 `wiki/queries/`：

| 条件 | 动作 |
|------|------|
| 被 ≥ 2 个其他页面引用 | 晋升合并到 `wiki/topics/` 成为正式主题页 |
| 用户明确表示"值得保留" | 晋升合并到 `wiki/topics/` |
| 超过 90 天且无任何引用 | 评估：有价值的补充分类后晋升；无价值的归档删除 |
| 一次性分析且已过时 | 直接删除 |

避免 `queries/` 变成半知识垃圾场。晋升时把原 query 的 `## 来源` 合并进新 topics 页的 `## 来源`。

---

## 6. Lint 规则

### 6.1 风险分级

| 级别 | 项目 | 处理方式 |
|---|---|---|
| 🟢 低风险 | 死链、frontmatter 缺失、index 覆盖 | `--fix` 可自动修 |
| 🟡 中风险 | 孤儿 raw（wiki_refs 空）、孤儿 page（无引用）、重复主题、重复 raw、图片超规、triage 老化 | 只报告，不自动改 |
| 🔴 高风险 | 大规模合并、目录重构、内容删除 | 走 ADR，不动 |

### 6.2 Lint 检查清单

**Raw 层：**
1. Frontmatter 完整性（raw 每个必填字段是否齐全）
2. `wiki_refs` 空 → 孤儿 raw
3. `source_url` 格式校验 — 非空时需符合 URL 格式
4. `published_at` 校验 — YYYY-MM-DD 格式，不晚于当前日期；缺失时从文件名自动提取
5. Triage 衰减扫描 — `status: triage` 按 `date` 计算存活天数：T+7 高亮，T+30 建议归档
6. `raw/` 根目录是否有游离文件
7. 孤儿图片检测 — raw 同名目录下未被任何 md 引用的图片
8. **空内容保护（正文过短）** — 估算正文 < 200 字（note/screenshot 类型 < 50 字）时告警，可能抓取失败或不完整
9. **空内容保护（失败关键词）** — 正文开头出现"404""权限不足""抓取失败"等关键词时告警
10. **live-source 时效提醒** — `fact_type: live-source` 的 raw：`last_checked` >90 天列入信息列表，>180 天建议重新检查或标记 stale（§4.5.4）

**Wiki 层：**
11. `wiki/` 里 `[[raw/...]]` 引用是否可达（死链）
12. `wiki/` 页面是否被 `wiki/index.md` 索引
13. 图片引用是否为相对路径
14. 图片引用路径检测 — md 中是否误用 `assets/` 或 `attachments/`
15. 根目录附件检测 — 根目录下是否存在 `assets/` 或 `attachments/`
16. **`last_verified` 日期校验** — >6 个月提示复核，>12 个月告警陈旧
17. **同标签更晚的 raw 未被 wiki 引用** — 共享 ≥2 个标签且 `published_at` 晚于 `last_verified` 的 raw 未被引用时提醒
18. **标签重叠但无交叉引用** — 共享 ≥2 个标签的 topics 页之间无任何引用时提醒，可能是标签太泛需删，也可能是漏引
19. **`contradicts` 字段校验** — 目标页面存在性 + 双向标注一致性
20. **空来源检测** — topics 页有 `## 来源` 章节但无任何实际 raw 文件引用时告警（query 页允许只引用 wiki 页面）
21. **重复主题检测** — `wiki/topics/` 下同名（stem 相同）主题页存在多份时告警
22. **wiki_refs 反向回填校验** — wiki 页 来源 引用了 raw，但 raw 的 `wiki_refs` 未回填该页时告警（双向溯源断裂）
23. **重复 raw 检测** — 同一篇文档被重复入库（clip-server 去重失效的兜底）。判据 = **同来源**（`source_id` 优先，否则规范化 `source_url`）+ **同规范化标题**（去日期前缀与 `-1`/`-v2` 后缀）+ **正文 4-gram Jaccard 相似度 ≥ 95%**，命中即 🟡。
    - 只看「同来源」不够：§4.5.2 允许一份文档镜像出多个子文档 raw——实测 TRAE 通识课 21 份、vibe-coding 19 份共享同一个 `source_url`，按"同来源只能一份"判定会把 45 份合法 raw 误报成错误
    - 只看文件名也不够：`-1`/`-2` 既可能是重复件、也可能是"上篇/下篇"，必须比对正文（实测合法镜像对相似度 0.01~0.09，重复件 0.99+，分离度极大，0.95 阈值极稳）
    - 若确为 §4.5.4 允许的小幅改版，在 ADR 中说明后即可豁免
24. **图片规范检测** — 聚合报告 `raw/` 下非 WebP 图片（.jpg/.jpeg/.png）数量与分布，以及单张 > 512KB 的超大图（§7.1「大图强制压缩」）。仅提示不自动转码——画质取舍需人工决定
25. **人工保护区规范**（§5.5.1）— wiki 页（index/log/queries 除外）须有且仅有一个 `## 我的思考` 章节，且位于 `## 来源` 之前。缺失 → 🟢（`--fix` 插入空占位）；重复或位置颠倒 → 🟡（需人工判断，避免误伤用户手写内容）
26. **topics 拆分线提醒**（§5.1）— `wiki/topics/` 根目录文件数 > 15 → ℹ️ 信息级提醒"该审视领域分布了"。lint 刻意**不识别领域**（近义归并是语义判断，正则不可靠，见 ADR-012）；归并由摄入第 4 步的 AI 做，拆不拆由人拍板走 ADR

### 6.3 安全阀

**`lint.py` 默认 dry-run，只输出报告：**
- `python3 lint.py` → 只报告问题
- `python3 lint.py --fix` → 仅自动修复 🟢 低风险项（当前支持：从文件名回填 `published_at`、为缺失的 wiki 页插入空 `## 我的思考` 占位；其余低风险项暂只报告）
- 🟡 中风险即使 `--fix` 也只报告
- 🔴 高风险一律 ADR

> 依赖：推荐 `pip install PyYAML`（见 requirements.txt）。未安装时 lint 使用内置的简化 frontmatter 解析器（支持扁平键值、行内/块式列表），两种路径输出一致。

---

## 7. 图片与媒体规范

### 7.1 图片

- 图片跟着 raw 走，放**同名子目录**下（不设顶层 `assets/`）：
  ```
  raw/articles/2026-07-20-karpathy-llm-wiki.md
  raw/articles/2026-07-20-karpathy-llm-wiki/
    └── screenshot.webp
  ```
- 引用用相对路径：`![说明](2026-07-20-karpathy-llm-wiki/screenshot.webp)`
- wiki 引用 raw 里的图片：`![说明](../../raw/articles/2026-07-20-.../screenshot.webp)`
- 大图强制压缩：WebP 优先，质量 75-80
  （lint 检查 24 会聚合报告非 WebP 图片与单张 > 512KB 的超大图）
- **尺寸规则（区分普通图与长截图）：**
  | 类型 | 判定 | 处理 |
  |---|---|---|
  | 普通图 | 宽高比 ≤ 3.0 | 最长边 ≤ **1920px**（横竖通用） |
  | 长截图 | 宽高比 > 3.0（手机滚动截屏典型 1:4~1:20） | **只限宽 ≤ 1440px，高度不动** |
  长截图**绝不能**按最长边缩放：1080×20000 的手机长图若套用 1920 上限会变成 1080×1920，每行压到约 100px，完全不可读。阈值 3.0 的选取依据：横版幻灯片实测聚集在 1:2.1~2.2，不会被误伤。
- **批量转码工具**：`tools/convert_to_webp.py`（默认 dry-run，`--apply` 执行）。自动按上表缩放、备份原图（保留相对结构）、同步改写 md 引用、并修正 "目录名 -N 后缀" 的错配死链
- **注意：剪藏时的图片是原样落盘的**（clip-server 不做任何缩放/转码），因此新剪藏的大图与长截图需**定期跑一次 `convert_to_webp.py`** 归一化；lint 检查 24 负责报告积压
- 每张图附原文链接（`> 原始来源: https://...` 或 `> 原始文件: 本地/xxx.png`）

### 7.2 录音 / 视频

| 文件类型 | 存储方式 |
|----------|----------|
| 转写文本 | 直接进 raw（`type: audio` 或 `type: chat`） |
| 原始录音 / 视频文件 | 上传云盘（飞书 / iCloud / 坚果云），raw 中只留链接 |

原始音视频文件不入 raw 目录，避免大文件污染仓库。raw 文件中备注云盘链接与本地备份位置（如有）：

```yaml
source: "飞书云盘:https://xxx.feishu.cn/file/xxx"
```

---

## 8. ADR（Architecture Decision Record）

**什么时候写：**
- 修改本文件规则、目录结构、frontmatter 格式
- 大规模重构 wiki 分类
- 采纳新的工作流

**放在哪：** `reviews/ADR-NNN-简短标题.md`（NNN 三位数字顺序编号）

**模板：** `templates/adr.md`

**状态：** `Proposed` → `Accepted` → `Superseded by ADR-XXX`（撤回时不删）

---

## 9. 操作速查

### 摄入（Ingest）
1. **抓取完整原文**：用 Web Clipper / lark-cli / WebFetch / PDF 提取等工具，把原始资料的完整内容抓下来
2. 保存到 `raw/{articles,notes,...}/YYYY-MM-DD-描述.md`（含完整 frontmatter + 完整原文快照，`wiki_refs: []`）
3. 从 raw 中提炼核心观点 → 写入 `wiki/topics/`（一份页面身兼摘要 + 概念）
4. **领域拆分检查**：新主题页落位时，判断它所属领域（含近义归并，如 vibe-coding ∈ ai-coding）在 `wiki/topics/` 根目录是否已 ≥5 个；是则按 §5.1 提议拆分子文件夹（`git mv` + ADR）。语义归并由 AI 做，拍板由人做——lint 检查 26 只做"根目录文件过多"的兜底提醒
5. 回填对应 raw 的 `wiki_refs`
6. 更新 `wiki/index.md` + `wiki/log.md`

### 查询（Query）
1. 先读 `wiki/index.md` 定位
2. 读 `wiki/` 相关页面
3. 综合回答，末尾附 `📚 来源：[[页面路径]]`
4. 默认只读 wiki，不读 raw（除非需要溯源/验证/冲突判断）
5. 有留存价值的回答 → `wiki/queries/YYYY-MM-DD-xxx.md`

### Lint（体检）
1. `python3 lint.py`（dry-run 预览）
2. 🟢 报告的可以 `--fix` / 🟡 只报告 / 🔴 走 ADR

### 回答规范
回答涉及 vault 已有知识时：
- 结尾附 `📚 来源：[[页面路径]]`
- 若回答本身有留存价值，询问是否归档为 `wiki/queries/YYYY-MM-DD-xxx.md`

---

## 10. 迁移与备份

- 本 vault 位置：本目录（`knowledge-vault/`）
- 迁移到其他机器：整目录复制即可，所有引用都是相对路径
- 其他 AI 接手：读本文件即可，涵盖所有规则与格式

---

## 11. 版本历史

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-16 | v2.12 | **topics 拆分半自动化**（ADR-012）：§9 摄入流程新增第 4 步「领域拆分检查」——新主题页落位时由 AI 判断所属领域（含近义归并，如 vibe-coding ∈ ai-coding）是否 ≥5 并提议拆分；lint 新增检查 26（根目录文件数 >15 → ℹ️ 提醒审视领域分布）。设计原则：阈值计数交给 lint，近义归并交给 AI，拆不拆由人走 ADR——lint 刻意不做领域识别，因为自由标签无受控词表，正则聚类对近义词（vibe-coding / ai-coding）又漏又错 |
| 2026-10-04 | v2.11 | **长截图尺寸规则**：用户提出"手机截取的长图怎么办"，暴露 v2.10 单一"最长边 1920"规则的盲区——手机长截图（1080×20000，宽高比 1:18）按最长边缩放会变成 1080×1920，每行压到约 100px，完全不可读。§7.1 改为双轨：普通图（宽高比 ≤3.0）限最长边 1920；长截图（宽高比 >3.0）**只限宽 1440、高度不动**。`convert_to_webp.py` 实装 `_plan_size()` 并新增 lint 检查（长截图宽度超 1440 告警）。同时确认一个事实：**clip-server 剪藏时图片原样落盘，不做任何缩放/转码**——故需定期跑转码工具归一化，已在 §7.1 写明 |
| 2026-10-04 | v2.10 | **文字型截图下采样**：v2.9 曾为幻灯片/代码截图设"保留原分辨率"例外，经用户要求实测对比后撤销——24 张超宽图（>1920px）缩到 1920px 后，同显示尺寸下肉眼差异轻微、可读性不受影响，体积再降 56%（2.4 MB → 1.05 MB），`raw/articles` 合计 **3.8 MB**（原始 46 MB） |
| 2026-10-04 | v2.9 | **存量图片全量转 WebP**：29 张非 WebP 图（44.8 MB）转码后 `raw/articles` 从 46 MB 降至 5.1 MB（省 42.9 MB）。新增 `tools/convert_to_webp.py`（dry-run 默认、自动备份、同步改写引用）。转码时发现并修复一处历史死链：14 张图被引用为 `...-1/image_NNN.jpg`，实际目录无 `-1` 后缀 → 修复后 articles 内图片引用可达数 15 → 29 |
| 2026-10-04 | v2.8 | **去重闸门前移**：剪藏入库此前是「先落盘、后判断」——`clip_from_url()` 写文件在前，`_smart_clip()` 查重在后，浏览器协助路径 `handle_browser_result()` 更是完全没查重，导致前端报"重复"但文件已入库。本次新增统一的写盘前闸门 `_preflight_duplicate()`，全部入库路径（浏览器插件 / 浏览器协助 / 后端直抓 / inbox）一律先判定再落盘；`clip_from_url()` 增加 `dry_run` 供"先抓取后判定"。**保护区收敛**：§5.5.1 由「`## 我的思考` 及其后全部内容」改为「仅 `## 我的思考` 一个章节」——原规则把 `## 来源` 也圈进保护区，导致 AI 无法履行 §5.4 的双向回链义务（`wiki_gen._merge_user_region` 一度只能连来源一起冻结）。配套：新增 lint 检查 25（保护区规范，缺失可 `--fix` 补空占位），为 18 个历史 wiki 页补上保护区，`wiki_gen` 重写来源累积逻辑（旧回链不因重写丢失） |
| 2026-10-04 | v2.7 | 补齐 lint 检查 23（重复 raw 检测）与 24（图片规范检测）——这两类问题此前只能靠人工发现。检查 23 的判据在当日评审中经一轮收敛：初版用「同来源 + 同标题 + 裸名/后缀组合 + 正文长度差」，会被"原件已归档"的场景绕过；终版改为「同来源 + 同标题 + **正文 4-gram Jaccard ≥95%**」，既补上该漏洞也更简洁（实测命中 5 份 Codex raw，相似度 99.8%；TRAE 子文档镜像与同名 README 全部静默）。检查 24 聚合报告非 WebP 图片与 >512KB 超大图。§6.1 中风险清单与 §7.1 同步补充 |
| 2026-10-03 | v2.6 | 文档合规收尾：§5.4 与 §5.4 编号重复改为 §5.5 / §5.5.1；§5.1「扁平存放」过时表述改为现状说明（含 `ai-coding/` 已启用）；§11 版本历史按时间序重排、补 v2.3 占位。配套执行 ADR-009（删除重复主题页 `wiki/topics/codex-agent-cookbook.md`）、ADR-010（归档 2 份重复 Codex raw） |
| 2026-10-03 | v2.5 | lint.py 修复：naive frontmatter 解析器正确支持 `[]`/行内/块式列表（无 PyYAML 环境不再产生误报）；全部路径统一正斜杠（修复 Windows 跨层比对失效）；`--fix` 实装（从文件名回填 `published_at`）；新增检查 10/21/22（live-source 时效、重复主题、wiki_refs 反向回填）；空来源检测仅限 topics 页；孤儿图片检测性能优化。§6.2 检查清单重新编号 |
| 2026-09-30 | v2.4 | Frontmatter 契约对齐剪藏工具：`TYPE_ENUM` 新增 `video`（并保留 `selection`），`VALID_RAW_FIELDS` 新增 `source_id`/`author`/`word_count`/`duration`（剪藏工具自动写入的可选元数据）；详见 ADR-008 |
| 2026-07-21 | v2.3 | （原记录缺失：v2.2 → v2.4 之间的修订内容未在任何 ADR 或 `wiki/log.md` 中留存，本次整理版本历史时无法还原。此处补占号，仅说明跳号，不臆测内容） |
| 2026-07-21 | v2.2 | Lint 新增 4 项 wiki 健康检查：last_verified 校验、同标签更晚 raw 未引用、同标签页面未互引、contradicts 双向性 |
| 2026-07-21 | v2.1 | **铁律修正**：Raw 必须为完整内容快照，禁止来源卡片模式；重写现有 3 份 raw 文件；新增一次性修正窗口（ADR-005） |
| 2026-07-21 | v2.0 | 合并 `schema.md` 到 `AGENTS.md`（规则 + 格式合一）；新增 queries 清理机制；阅读顺序简化 |
| 2026-07-21 | v1.4 | 新增 triage 简化版衰减机制（T+7 高亮 / T+30 建议归档）；新增录音/视频存储规则 |
| 2026-07-21 | v1.3 | Raw frontmatter 新增 `source_url` + `published_at`；新增 topics 拆分触发条件（同领域 ≥ 5 文件） |
| 2026-07-21 | v1.2 | wiki 目录合并：`concepts/` + `entities/` + `summaries/` → `topics/`；`index.md`/`log.md` 移入 `wiki/` |
| 2026-07-21 | v1.1 | `pages/` → `wiki/`；删除冗余的顶层 `assets/` |
| 2026-07-21 | v1.0 | 首版：从 `analysis/wiki/` 迁移到 `桌面/knowledge-vault/`；升级为治理契约 |
| 2026-07-20 | v0.1 | 首次建立 `analysis/wiki/`，仅有 schema.md |
