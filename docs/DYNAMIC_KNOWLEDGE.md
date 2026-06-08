# 动态知识检索（Dynamic Knowledge）

## Goal

确保各节点（尤其是 SQL 生成节点）只使用“有证据支撑”的语法、GUC、Hint、对象定义约束，避免编造。

## Runtime behavior

1. 节点先检查已知证据（来自 shared/stage cache）。
2. 若证据不足，节点调用知识索引 tool 检索所需主题。
3. tool 优先通过 Claude Code CLI 从本地资料目录/用例目录检索证据，并返回结构化 `KnowledgeItem`。
4. 若 Claude 检索失败，tool 会在 UI 打 warning，并可降级为本地目录扫描（token 匹配 + 片段抽取）。
5. 节点可在同一 run 内多轮检索，直到证据充分或确认缺失。
6. 若证据仍缺失，SQL 必须输出 TODO/uncertainty 注释，明确“不确定”，不得补齐编造语法。

## Current tools

- `retrieve_database_knowledge(...)`：核心 tool，返回结构化证据条目（`KnowledgeItem`）与 `missing_topics`。

## Notes to fill later

- Retrieval ranking rules:
- Distillation rules:
- External knowledge source extensions:

## 另见

- `docs/KNOWLEDGE_INDEX.md`：知识索引能力的完整算法流程与配置说明。
