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

Planner 当前使用 **fast path**（快速路径），仅执行 draft 后直接提升为正式计划：

1. `PLANNER_DRAFT`（AgentNode，强制先调知识工具）
   - 根据用户输入生成第一版结构化计划（`PlannerDraft`）
   - 通过 `force_first_tool=True` 确保先检索知识再规划
2. Draft → `PlannerOutput` 本地转换（`_draft_to_output`）
   - 跳过 `PLANNER_CRITIC` 和 `PLANNER_FINALIZER`
   - 显著降低延迟和失败概率
3. `PLANNER_REVISION`（AgentNode）
   - 当用户不批准总计划时，用于修订总计划

原来的 `PLANNER_CRITIC` 和 `PLANNER_FINALIZER` 节点虽有代码绑定和 prompt 文件，
但当前默认不执行。仅在需要更重的对抗/修订链路时再启用。

### 2.3 Planner 知识缓存同步

Draft 完成后，系统会将 `ctx.shared_state["knowledge_cache"]` 中的知识数据
同步到 `PlannerOutput.cached_knowledge`，供后续阶段复用。缓存 key 包括：

- `planner` — draft 期间检索到的知识
- `shared` — 跨阶段共享知识
- `setup` / `ddl` / `core` — 各阶段知识（初始为空，逐步填充）

### 2.4 用户审阅总计划

在 planner 产出 `PlannerOutput` 后，CLI 会展示总计划摘要。

用户有三种选择：

1. 批准 → **自动保存 checkpoint（`planner` 阶段）**
2. 输入反馈 → 发送给 `PLANNER_REVISION` 修订后重新展示
3. 退出 → 已有 checkpoint 保留，下次可 `--resume` 继续

### 2.5 子计划生成与审阅

总计划批准后，系统先生成全部三个子计划（SETUP / DDL / CORE），再逐一审阅：

1. `build_phase_plan("SETUP")` → `build_phase_plan("DDL")` → `build_phase_plan("CORE")`
2. 审阅 `SETUP` 子计划 → 批准后 **保存 `setup_plan` checkpoint**
3. 审阅 `DDL` 子计划 → 批准后 **保存 `ddl_plan` checkpoint**
4. 审阅 `CORE` 子计划 → 批准后 **保存 `core_plan` checkpoint`

如果用户不批准某个子计划：

1. 对应计划和反馈会发给对应 refiner（`SETUP_PLAN_REFINER` 等）
2. refiner 修订后再次展示
3. 直到批准后保存 checkpoint

## 3. 阶段执行

审批全部完成后，系统分两步执行：

1. **并行执行** `SETUP` 和 `DDL`（通过 `asyncio.gather`）
2. **串行执行** `CORE`（需要 DDL 的 `generated_sql` 和 `evidence_checklist`
   作为上游输入 `upstream_artifacts`）

每个阶段收到的输入包括：

1. 用户原始输入
2. 全局总计划
3. 当前阶段计划
4. 当前阶段已缓存知识
5. Planner 全量结构化输出
6. 知识使用约束说明

## 4. 动态知识取证阶段

每个 `AgentNode` 都绑定了一类工具：

1. `retrieve_database_knowledge(stage, topics, reason)`
   - 核心工具，调用 Claude Code CLI 或本地索引检索语法证据
2. 在 `_enrich_payload_with_knowledge` 中预先根据 `required_topics` 拉取缓存

节点的预期行为是：

1. 先读取已有的 `cached_knowledge`
2. 如果缺少某个语法依据，调用 `retrieve_database_knowledge`
3. 知识缓存按 `stage` 隔离，`shared` 跨阶段共享
4. 直到关键语法点都有 evidence，再开始写 SQL

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

### 8.1 日志（新目录结构）

每次运行在 `logs/` 下创建以 `run_id` 命名的子目录：

```
logs/{run_id}/
  run.log                    # 标准 Python 日志
  run.jsonl                  # 结构化事件日志（JSONL，逐行追加）
  user_input.json            # 用户原始输入（供 checkpoint 恢复）
  checkpoint_manifest.json   # checkpoint 阶段清单
  planner/planner_output.json       # 阶段 1 checkpoint
  setup_plan/planner_output.json    # 阶段 2 checkpoint
  ddl_plan/planner_output.json      # 阶段 3 checkpoint
  core_plan/planner_output.json     # 阶段 4 checkpoint
```

JSONL 事件日志记录以下关键事件：

| 事件类型 | 触发时机 |
|----------|----------|
| `user_review` | 用户审阅决策（scope + decision） |
| `json_parse_retry` | JSON 解析失败时的重试（node + attempt + error） |
| `json_parse_failed` | 所有解析策略均失败（node + raw_text 全文） |
| `try_heal_strategy_0/1/2` | `_try_heal` 各策略的尝试结果 |
| `phase_fallback` | 阶段节点超时/出错触发 fallback |
| `sql_recovered_from_phase_result` | 从非 `generated_sql` 字段回收的 SQL |
| `workflow_complete` | 工作流完成（output_path + sql_path + success） |

### 8.2 产物

主流程输出：

- `output/<run_id>.json` — 完整 `FinalWorkflowOutput`
- `output/<run_id>.sql` — 合并后的 SQL

样例流程输出：

- `sample_cases/outputs/<run_id>.json`
- `sample_cases/transcripts/<run_id>.json`

## 9. 错误重试与 JSON 修复机制

### 9.1 AgentNode 多策略 JSON 修复

当模型输出的 JSON 被 SDK 拒绝时（`ModelBehaviorError`），不会立即失败，
而是进入 `_try_heal` 三条恢复策略：

| 策略 | 方法 | 说明 |
|------|------|------|
| 0 | `json.JSONDecoder.raw_decode` | 从原始文本中提取第一个合法 JSON 对象 |
| 1 | `model_validate_json` + 去 markdown | 移除 ` ```json ... ``` ` 包裹后解析 |
| 2 | `json.loads` + `model_validate` | 标准 JSON 加载后 Pydantic 校验 |

### 9.2 Streaming 阶段的错误捕获

`_single_agent_run` 在两个阶段分别捕获 `ModelBehaviorError`：

1. **`stream_events()` 迭代阶段** — 流式收集模型输出文本时，SDK 可能在校验
   JSON 时抛出异常。静默捕获，不中断 UI 展示。
2. **`result.final_output` 阶段** — 获取最终解析结果时，SDK 再次校验。
   捕获后统一交由 `_try_heal` 恢复。

### 9.3 重试与会退

`AgentNode.run()` 的 retry 循环：

1. 最大重试次数：`MAX_JSON_RETRIES`（当前为 1 次）
2. 每次重试前记录 `json_parse_retry` 事件（含完整错误信息）
3. 所有重试耗尽后，若为 DeepSeek provider 则走 `_fallback_two_phase` 会退路径
4. 最终失败时记录 `json_parse_failed` 事件，含完整 `raw_text`（不截断）

### 9.4 阶段执行超时与 Fallback

每个阶段有独立的超时控制（`executor.phase_timeout_seconds`）：

1. 阶段节点通过 `asyncio.wait_for` 限时执行
2. 超时或异常时，若 `allow_phase_fallback=True`，调用 `build_fallback_phase_result`
   生成确定性的兜底 SQL（使用 `planner_output` 和 `phase_plan` 中的 JSON 结构）
3. fallback 事件记录到 JSONL（`phase_fallback`）

## 10. Checkpoint / Resume 机制

### 10.1 设计动机

完整的一次 SqlMate 运行需要多次 LLM 调用（planner + 3 个 phase plan + 3 个
SQL 生成），耗时较长。中途修改后需从头重跑会浪费大量时间和 API 调用成本。

### 10.2 Checkpoint 阶段

在用户每次批准计划后自动持久化当前状态：

| 阶段 | 触发时机 | 保存内容 |
|------|----------|----------|
| `planner` | 批准总计划 | PlannerOutput（global_plan + 3 个 outline） |
| `setup_plan` | 批准 SETUP 子计划 | PlannerOutput（含全部 3 个 PhasePlan） |
| `ddl_plan` | 批准 DDL 子计划 | PlannerOutput（同上，DDL 已 review） |
| `core_plan` | 批准 CORE 子计划 | PlannerOutput（同上，全部已 review） |

### 10.3 恢复运行

```bash
# 从最近的 checkpoint 恢复
python -m app.main --config config/settings.example.yaml --resume 20260610_143021_123

# 强制清除已有 checkpoint 重新开始
python -m app.main --config config/settings.example.yaml --resume 20260610_143021_123 --force
```

恢复逻辑：

- 读取 `checkpoint_manifest.json` 确定已完成阶段
- 加载最近 checkpoint 的 `PlannerOutput`
- 跳过已完成的 planning 阶段，从断点继续
- 如果全部 4 个 planning 阶段都已完成，直接进入 SQL 生成

### 10.4 实现文件

- `app/services/checkpoint.py` — `CheckpointManager` 类
- `app/workflow/orchestrator.py` — `run()` 中的条件跳过逻辑
- `app/main.py` — `--resume` / `--force` CLI 参数

## 11. 当前限制

1. 真正的业务效果仍依赖 prompt 内容质量
2. 当前执行器（`MockSqlExecutor`）还未接真实数据库
3. 修复回路（`SQL_REPAIR`）尚未接入主流程

## 12. 后续演进方向

1. 将知识服务扩展为多源检索
2. 为 planner 引入多 agent 投票/对抗
3. 为阶段执行增加真实 DB 执行和 repair
4. 为 CLI 增加更强的状态展示与历史回看
5. checkpoint 粒度扩展到 SQL 生成阶段

