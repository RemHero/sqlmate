<!-- This doc is intentionally written in Chinese. -->
# 知识索引（Knowledge Index）

本文档描述 SqlMate 的“知识索引”能力：planner/phase-planner 如何从本地资料与 SQL 用例仓中检索证据、如何缓存与共享证据，以及 SQL 生成阶段如何被 evidence 约束，避免编造语法。

当前版本有一个明确原则：知识索引 tool 的职责是“定位并摘取证据原文”，不是“做高度摘要”。尤其当上游要的是某个 SQL 语法知识时，返回结果应优先包含语法定义、命令格式、参数说明、限制条件、示例 SQL 的原文片段。

## 目标

- 在总 planner 与各阶段 planner 需要时，检索“数据库特定知识”的证据。
- 强制 SQL 生成对语法/GUC/Hint/对象定义给出证据来源（evidence）。
- 保留不确定性：当找不到证据时，SQL 必须明确写注释说明不确定，而不是“凭常识补齐/编造语法”。

## 总体架构

- 知识检索以 Agents SDK tool 形式暴露：`retrieve_database_knowledge`。
- tool 内部委托 `KnowledgeService.retrieve_external()` 通过 Claude Code CLI 非交互调用进行检索。
- 检索结果以 `KnowledgeItem` 列表形式回传，并写入 shared/stage cache 供后续节点使用。
- planner、phase-planner、以及 SQL 生成节点均可在一次 run 内多次调用 tool（按需多轮检索）。

## 运行流程（算法）

1. **Agent 决定要检索的知识**
   - 总 planner：检索“特性规格/行为约束/边界条件”等，用于正确拆解需求（不关注具体 SQL 落地细节）。
   - phase planner：检索“语法写法/GUC/对象 DDL 关键限制/示例 SQL”等，用于把 planner 的测试要点落到本阶段可执行计划。
   - SQL 生成：在真正落 SQL 前补齐证据；仍缺失则必须输出不确定性注释并避免编造。

2. **tool 调用**
   - 节点调用 `retrieve_database_knowledge(stage, topics, search_goal, knowledge_sources, required_evidence)`。
   - `knowledge_sources` 可选：`docs`、`sql_examples`（或两者）。

3. **通过 Claude Code CLI 外部检索**
   - `KnowledgeService.retrieve_external()` 组装 prompt：
     - 加载 `node_prompts/KNOWLEDGE_INDEX.md`
     - 追加 JSON payload：`<KnowledgeIndexRequest>...</KnowledgeIndexRequest>`
   - 以非交互方式执行 `claude -p`，并显式放行本地目录读取：
     - `--permission-mode bypassPermissions`
     - 若包含 `docs`：`--add-dir <doc_knowledge_dir>`
     - 若包含 `sql_examples`：`--add-dir <sql_examples_dir>`
   - 要求 Claude 仅输出一个 JSON 对象（不得输出 Markdown），字段包括：
     - `found_items[]`（每项必须带 `source` 文件路径、`evidence_type`、`confidence`）
     - `missing_topics[]`
     - `distilled_summary`
   - `found_items[].content` 的语义不是“摘要”，而是“尽量保留原始表述的证据摘录”。
   - 如果同一 topic 在多个章节、多个语法段、多个限制段中均有命中，应拆成多条 `found_items` 返回，而不是压缩成一句话。

4. **解析与打包**
   - 成功：解析 JSON，生成标准 `KnowledgeBundle`，记录一次 retrieval round：
     - `tool=claude_code_cli`
     - `searched_sources=[doc_knowledge_dir, sql_examples_dir, ...]`

5. **失败与降级**
   - Claude 调用失败（超时/非 0 退出码/输出无法解析）：
     - `retrieve_external()` 返回“空 bundle + missing_topics”（不伪装为成功）。
   - tool 层会在 UI 打 warning（明确提示 Claude 检索失败），并可降级为本地扫描：
     - 在配置目录内做 token 匹配扫描
     - 返回带文件路径与原文片段的 fallback evidence
     - fallback 也尽量返回更多命中项，而不是只保留极少数结果

## 当前检索策略

- 如果 topic 明确是语法类需求，例如 `vacuum syntax`、`partition syntax`、`join syntax`，必须优先定位语法定义、命令格式、参数说明和示例。
- 如果 topic 明确是 GUC/参数类需求，必须优先定位参数名、默认值、取值范围、行为说明。
- 如果 topic 明确是限制类需求，必须优先定位限制条件、边界、例外、兼容性说明。
- `distilled_summary` 只作为导航摘要，不能替代 `found_items` 中的证据本体。
- 返回内容宁可偏多，也不要因为追求简洁而丢失关键语法段。

6. **缓存合并与共享**
   - tool 返回结果并写入 cache。
   - 总 planner 检索到的“特性证据”可以进入 shared cache，并在 phase planner 输入中继承。

## Evidence 约束（SQL 生成强制要求）

在 `SETUP` / `DDL` / `CORE` 的 prompt 里，SqlMate 强制执行 evidence-first：

- 任何 SQL 语法、GUC、Hint、对象定义关键选择，都必须能对应至少一条 `KnowledgeItem`（带具体 `source`）。
- 若证据缺失：
  - 不允许编造语法。
  - 必须在相关 SQL 行附近写清晰的 TODO/uncertainty 注释，说明“语法/资料不确定”。
  - 尽可能让脚本整体仍可运行（用最小、安全的占位实现）。

## 配置项

配置位于 `knowledge:`：

- `enable_claude_index`：是否启用 Claude 外部检索。
- `claude_command`：Claude CLI 命令，默认 `claude`。
- `claude_permission_mode`：默认 `bypassPermissions`，保证非交互目录读取不被权限卡住。
- `claude_use_add_dir`：为知识目录追加 `--add-dir`。
- `claude_timeout_seconds`：Claude 调用超时。
- `claude_max_output_chars`：输出截断阈值。
- `doc_knowledge_dir`：本地资料目录（默认：`/home/remhero/shared/ai/SqlMate/worflow/txzn`）。
- `sql_examples_dir`：SQL 用例仓目录（可先为空目录，后续再填充）。

## 关键实现文件

- `app/tools/knowledge_index.py`：Agents SDK tool 封装（`retrieve_database_knowledge`）。
- `app/services/knowledge.py`：Claude 检索 + 本地 fallback 扫描。
- `node_prompts/KNOWLEDGE_INDEX.md`：知识索引子任务 prompt 合约。
- `app/schemas/models.py`：`KnowledgeItem` / `KnowledgeBundle` / 检索轮次记录结构。

## 最近调整

- 强化了 `node_prompts/KNOWLEDGE_INDEX.md`，要求 Claude 先判断证据类型，再定位章节，再返回原文摘录。
- 放宽了 fallback 返回上限，避免只返回极少条目。
- 放宽了单条片段的长度上限，减少命中到语法章节却被过度截断的问题。
