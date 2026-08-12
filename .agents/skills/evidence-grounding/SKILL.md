---
name: evidence-grounding
version: 1.0.0
description: 让知识解释和答案要点能够追溯到用户材料或外部来源
keywords: 证据 引用 来源 事实 不确定性
enabled: true
---

# Evidence Grounding

## 目标

确保草稿中的硬事实、数字、定义和因果关系均有可定位的证据。

## 规则

1. 用户材料证据记录 `source_id`、`start_char`、`end_char` 和短引文。
2. 外部网页证据必须记录 URL 和检索时间，且仅在用户授权外部检索时使用。
3. 推断与原文陈述分开表达。
4. 证据冲突时保留冲突，不静默合并为单一结论。
5. 引文仅保留校验所需的最短片段。
