# ADR-008: 对齐 Frontmatter 契约与剪藏工具输出

- Date: 2026-09-30
- Status: Accepted

## Context

`tools/clip-server.py` 入库网页 / 视频 / 文档时，会写出剪藏工具专属的 frontmatter 字段：`type: video`，以及 `source_id` / `author` / `word_count` / `duration`。但 `AGENTS.md` 的 type 枚举缺少 `video`，`lint.py` 的 `VALID_RAW_FIELDS` 也未收录这四个字段。结果：剪藏工具自己生成的内容，过不了项目自带的 `lint.py`（报 `raw:bad-type` 与 `raw:extra-fields`）。此外 `AGENTS.md` 早已把 `selection` 列入 type 枚举，而 `lint.py` 的 `TYPE_ENUM` 遗漏了它，存在规范内部不一致。

## Decision

- `lint.py` 的 `TYPE_ENUM` 增加 `video` 与 `selection`，与 `AGENTS.md` 对齐。
- `lint.py` 的 `VALID_RAW_FIELDS` 增加 `source_id`、`author`、`word_count`、`duration`，承认剪藏工具自动写入的可选元数据。
- `AGENTS.md` §4.2 的 type 枚举补 `video`，并新增 §4.2.1 说明这四个可选字段的语义。

## Consequences

- 剪藏产物不再触发 `raw:bad-type` / `raw:extra-fields`，契约自洽。
- 人工手写的 raw 仍无需这些字段（可选）。
- 后续若剪藏工具新增字段，需同步更新 `lint.py` 的 `VALID_RAW_FIELDS` 与 `AGENTS.md`，避免再次漂移。

## Alternatives Considered

- 方案 B：反过来改 clip-server，去掉 `video` 类型、改用已有 type 并丢弃这四个字段。否决——视频是独立媒体类型，且这些元数据（作者 / 时长 / 字数）有留存价值，强行丢弃会损失信息。
- 方案 C：把 `lint.py` 的 naive fallback 当成「无 PyYAML 也正确」。否决——fallback 会把 YAML 列表当字符串逐字符迭代，产生虚假 `dead-contradict`；PyYAML 应视为必需依赖。
