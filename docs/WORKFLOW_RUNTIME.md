# Workflow Runtime

## 1. 运行入口

程序入口位于 `app/main.py`。

启动命令示例：

```bash
/home/remhero/bin/.venv/bin/python -m app.main \
  --config config/settings.example.yaml \
  --input-file tests/demo_input.json
```

自动审批模式：

```bash
/home/remhero/bin/.venv/bin/python -m app.main \
  --config config/settings.example.yaml \
  --input-file tests/demo_input.json \
  --approval-mode auto
```

## 2. 运行主线

### 2.1 初始化阶段

程序启动后依次完成：

1. 读取 `.env`
2. 解析命令行参数
3. 读取 YAML 配置
4. 初始化日志系统
5. 初始化 CLI UI
6. 读取输入 JSON
7. 初始化 PromptLoader
8. 初始化 KnowledgeService
9. 初始化 ProviderRegistry
10. 组装 planner 和 orchestrator

### 2.2 Planner 阶段

Planner 由四部分组成：

1. `PLANNER_DRAFT`
   - 根据用户输入生成第一版结构化计划
2. `PLANNER_CRITIC`
   - 审查 draft，指出覆盖不足、假设风险、知识缺口
3. `PLANNER_FINALIZER`
   - 合并 draft 与 critic 结果
   - 在必要时进一步调用知识工具
   - 输出正式 `PlannerOutput`
4. `PLANNER_REVISION`
   - 当用户不批准总计划时，用于修订总计划

### 2.3 Planner 阶段的知识预热

在 finalizer 之前，系统会根据 draft 中的：

- `setup_outline.required_topics`
- `ddl_outline.required_topics`
- `core_outline.required_topics`

先从知识库拉取一轮基础 evidence，并写入 `knowledge_cache`。

这样做的目的不是一次性把所有上下文塞给模型，而是：

1. 给 finalizer 一个最小可用证据集
2. 让后续阶段在已有缓存基础上继续增量检索

### 2.4 用户审阅总计划

在 planner 产出 `PlannerOutput` 后，CLI 会展示总计划摘要。

用户有三种选择：

1. 批准
2. 输入反馈
3. 退出

如果输入反馈：

1. 当前总计划和用户反馈会发给 `PLANNER_REVISION`
2. 系统重新生成结构化总计划
3. 再次展示给用户
4. 直到用户批准

### 2.5 用户审阅子计划

总计划批准后，会分别展示：

1. `SETUP` 子计划
2. `DDL` 子计划
3. `CORE` 子计划

如果用户不批准某个子计划：

1. 对应计划和反馈会发给对应 refiner
2. refiner 修订后再次展示
3. 直到批准

## 3. 并行执行阶段

审批全部完成后，系统会并行执行：

1. `SETUP`
2. `DDL`
3. `CORE`

并行调用通过 `asyncio.gather` 完成。

每个阶段收到的输入包括：

1. 用户原始输入
2. 全局总计划
3. 当前阶段计划
4. 当前阶段已缓存知识
5. Planner 全量结构化输出
6. 知识使用约束说明

## 4. 动态知识取证阶段

每个 `AgentNode` 都绑定了三类工具：

1. `inspect_known_evidence(stage)`
2. `list_available_topics(stage)`
3. `retrieve_knowledge(stage, topics, reason)`

节点的预期行为是：

1. 先看已有 evidence
2. 如果缺少某个语法依据，就列出要检索的 topic
3. 调用 `retrieve_knowledge`
4. 如果中途又发现新的不确定点，继续检索
5. 直到关键语法点都有 evidence，再开始写 SQL

## 5. 阶段结果产出

每个阶段输出 `PhaseResult`，包含：

1. `stage`
2. `plan_summary`
3. `knowledge_used`
4. `generated_sql`
5. `validation_notes`
6. `unresolved_risks`
7. `evidence_checklist`

## 6. 最终合并

三个阶段完成后：

1. Orchestrator 按固定顺序拼接 SQL
2. 当前顺序为：
   - `SETUP`
   - `DDL`
   - `CORE`
3. 拼接后的 SQL 进入执行器

## 7. 执行器阶段

当前默认执行器是 `MockSqlExecutor`。

它目前只做最低限度的校验：

1. 如果 SQL 为空，则判定失败
2. 如果 SQL 非空，则判定通过

后续可以替换成：

1. 真实数据库执行器
2. 带 repair loop 的执行器
3. 带阶段错误归因的执行器

## 8. 日志和产物

### 8.1 日志

每次运行会生成：

1. `logs/<run_id>.log`
2. `logs/<run_id>.jsonl`

### 8.2 产物

主流程输出：

- `output/<run_id>.json`

样例流程输出：

- `sample_cases/outputs/<run_id>.json`
- `sample_cases/transcripts/<run_id>.json`

## 9. 当前限制

1. 真正的业务效果仍依赖 prompt 内容质量
2. 当前知识库是本地 JSON KV Store
3. 当前执行器还未接真实数据库
4. 修复回路尚未接入真实环境

## 10. 后续演进方向

1. 将知识服务扩展为多源检索
2. 为 planner 引入多 agent 投票
3. 为阶段执行增加真实 DB 执行和 repair
4. 为 CLI 增加更强的状态展示与历史回看

