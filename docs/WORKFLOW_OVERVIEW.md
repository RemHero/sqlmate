# Workflow Overview

## Current Architecture

SqlMate 当前采用“两阶段规划 + 三阶段生成”的链路：

1. 用户输入进入工作流。
2. `PLANNER_DRAFT` 生成高层测试规格：
   - `global_plan`
   - `setup_outline`
   - `ddl_outline`
   - `core_outline`
3. 用户可在 CLI 中审阅总 planner 输出，并通过 `PLANNER_REVISION` 修订。
4. `SETUP_PLAN_REFINER`、`DDL_PLAN_REFINER`、`CORE_PLAN_REFINER` 分别基于：
   - 用户原始需求
   - `global_plan`
   - 对应 `phase_outline`
   - 共享对象契约
   - 本阶段知识缓存
   生成各自的详细 `PhasePlan`。
5. 用户可逐阶段审阅 `PhasePlan`，并继续修订。
6. `SETUP`、`DDL`、`CORE` 三个执行节点根据各自 `PhasePlan` 生成 SQL。
7. 三个阶段的 SQL 被合并为最终 SQL bundle。
8. 合并结果写入：
   - `output/<run_id>.json`
   - `output/<run_id>.sql`
9. 当前默认使用 mock executor 做链路验收。

## Design Intent

这条链路的核心目标是：

- 总 planner 只负责拆用户需求、总结测试目标、定义共享对象契约和阶段边界。
- 子阶段 planner 再根据本阶段职责，继续结合语法知识和对象需求细化 plan。
- 最终 SQL 生成节点只负责落实 plan，不负责重写用户目标。

## Notes

- 当前主链路已经关闭了严格的 `sql_quality` 阶段校验，优先保证流程走通和 plan 驱动生成。
- `PLANNER_CRITIC` / `PLANNER_FINALIZER` 目前处于 fast path 外，主链路不依赖它们。

## Knowledge Index

当前工作流已接入“知识索引”能力：

- planner / phase planner / SQL 生成节点都可以通过 tool 检索证据。
- Claude Code CLI 用于从本地资料目录与 SQL 用例仓目录中检索（非交互）。
- 若 Claude 检索失败，系统会提示 warning，并可降级为本地目录扫描作为 fallback evidence。

详见：`docs/KNOWLEDGE_INDEX.md`。
