# node_prompts

## 目录作用

保存所有节点的 Prompt 文件，运行时按节点名动态加载。

## 当前分类

### 当前主流程直接使用

- `PLANNER_DRAFT.md`
- `KNOWLEDGE_INDEX.md`
- `PLANNER_REVISION.md`
- `SETUP_PLAN_REFINER.md`
- `DDL_PLAN_REFINER.md`
- `CORE_PLAN_REFINER.md`
- `SETUP.md`
- `DDL.md`
- `CORE.md`

### 当前代码仍有绑定，但默认主路径不会执行

- `PLANNER_CRITIC.md`
- `PLANNER_FINALIZER.md`
- `SQL_REPAIR.md`

`PLANNER_CRITIC` 和 `PLANNER_FINALIZER` 当前会在启动时实例化，但 `PlannerPipeline.run()` 走的是 fast path，
默认直接把 draft 提升为 `PlannerOutput`，不会执行这两个节点。

`SQL_REPAIR` 目前只在配置中保留 binding，主流程没有接入调用链。

`KNOWLEDGE_INDEX` 不直接作为 LLM 节点运行；它是 `retrieve_database_knowledge` 工具调用 Claude Code CLI 时使用的检索任务 prompt。

## 维护建议

1. Prompt 尽量只描述角色、目标、约束、输出格式
2. 不要把配置类信息硬编码进 prompt
3. 修改节点名时同步更新代码绑定与 prompt 文件名
