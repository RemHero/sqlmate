# SqlMate Mid-Project Architecture

## Goal

SqlMate 面向数据库核心研发/测试人员，输入一段自然语言测试需求，输出一份按阶段组织、可执行、可验证的 SQL 测试脚本。

系统的目标不是做单一特性的模板化 SQL 拼接，而是把用户需求逐步转化为：

1. 高层测试规格
2. 阶段化详细计划
3. 最终 SQL 产物

## Core Design Philosophy

### 1. Planner 先做需求分解，不直接写 SQL

主 planner 的职责是从整体拆解用户需求，识别：

- 用户真正要测试的功能点和语义点
- 必须覆盖的测试要点
- 组合维度
- 成功判定
- 共享对象契约
- 风险和待求证问题

这一阶段只做“测试规格规划”，不做具体语法落地。

### 2. 三个子阶段分别细化自己的计划

`SETUP`、`DDL`、`CORE` 不直接吃一个已经写死的总计划执行，而是先各自生成自己的 `PhasePlan`。

这样做的原因是：

- 各阶段关注点不同
- 各阶段需要的知识类型不同
- 各阶段对语法细节的依赖不同
- 总 planner 不适合替每个阶段过早做细节决策

所以当前架构是：

- 总 planner 负责拆需求和定方向
- 子阶段 planner 负责结合本阶段知识继续细化
- 执行节点负责把 plan 落成 SQL

### 3. 规划与生成解耦

SqlMate 当前明确分离：

- 规划节点：产出结构化 plan
- 执行节点：产出 SQL

这保证了：

- 用户可以在 SQL 生成前审 plan
- 系统可以在阶段边界上修订
- SQL 生成更容易保持可控

## Current Workflow

### Step 1: User Request Intake

用户输入自然语言需求，内容可能包括：

- 测试目的
- 语法点
- 组合要求
- 验证要求
- 限制条件
- 对象偏好

### Step 2: Global Planning

`PLANNER_DRAFT` 生成 `PlannerDraft`，核心产物为：

- `global_plan`
- `setup_outline`
- `ddl_outline`
- `core_outline`

这里的重点是：

- 不写具体 SQL
- 不固定 DDL 细节
- 不提前锁死 CORE 查询写法
- 只定义整体测试规格和阶段职责

### Step 3: Planner Review

用户可以在 CLI 中查看 planner 输出，并通过 `PLANNER_REVISION` 修订。

修订的重点是：

- 范围是否完整
- 测试点是否遗漏
- 对象契约是否合理
- 阶段职责是否清晰

### Step 4: Phase Planning

系统分别为三个阶段生成详细 `PhasePlan`：

- `SETUP_PLAN_REFINER`
- `DDL_PLAN_REFINER`
- `CORE_PLAN_REFINER`

每个阶段的输入通常包括：

- `user_input`
- `global_plan`
- 对应 `phase_outline`
- `shared_contract`
- 本阶段 `cached_knowledge`

每个阶段在这里完成“本阶段内的二次规划”。

### Step 5: Phase Review

用户可以逐阶段审阅 plan，并要求修订。

这一步可以防止：

- 阶段职责越界
- 测试点被错误细化
- 对象设计和最终测试目标脱节

### Step 6: SQL Generation

三个执行节点根据各自 `PhasePlan` 生成 SQL：

- `SETUP`
- `DDL`
- `CORE`

职责分工如下：

- `SETUP`：环境、会话、schema、开关、前置检查
- `DDL`：对象定义与对象支撑能力
- `CORE`：最终测试 SQL、验证 SQL、覆盖自检

### Step 7: Merge And Output

工作流将三个阶段的 SQL 合并为一个最终 SQL bundle，并输出：

- `output/<run_id>.json`
- `output/<run_id>.sql`

## Data Contract Summary

### `global_plan`

面向全流程的高层测试规格，包含：

- 测试目标
- 受测特性
- 必测项
- 共享对象契约
- 验证策略
- 风险

### `required_setup_objects`

这个字段现在被设计为“共享对象契约”，不是死板的 DDL 结构模板。

它的重点是说明：

- 需要哪些跨阶段对象
- 这些对象应具备什么测试特性
- 它们为什么存在

它不要求强行包含数据库对象，也不要求固定主键/唯一键等字段，除非用户需求确实需要。

### `phase_outline`

面向阶段的高层纲领，描述：

- 阶段目标
- 覆盖重点
- 阶段边界
- 成功标准
- 留待细化的问题

### `phase_plan`

面向单阶段的详细执行计划，描述：

- 该阶段最终要交付什么
- 要覆盖哪些测试点
- 依赖什么输入
- 产出什么输出
- 需要确认哪些语法知识
- 目前还有哪些知识缺口

## Prompt Strategy

当前 prompt 设计遵循这几条原则：

1. Prompt 必须通用，不能写死某一个具体测试任务。
2. 总 planner prompt 偏“测试规格规划”，不偏“语法实现”。
3. 子阶段 plan prompt 偏“阶段内细化”。
4. 执行节点 prompt 偏“严格落实 plan 并生成 SQL”。
5. 任何节点都不应因为字段存在而虚构用户没要求的对象或约束。

## Current Runtime Characteristics

### Strengths

- 需求分解与 SQL 生成已经解耦
- 用户可在 planner 和 phase plan 两层做 review
- 三阶段职责边界已经基本明确
- 输出可同时保留结构化计划和最终 SQL

### Current Tradeoffs

- `sql_quality` 主链路校验目前被弱化，优先保证流程走通
- 部分 schema 字段仍偏宽松，需要后续继续收敛
- `PLANNER_CRITIC` / `PLANNER_FINALIZER` 仍未回到主链路

## Current End-to-End Path

从用户需求到最终 SQL 的当前路径是：

1. 用户提交自然语言测试需求
2. `PLANNER_DRAFT` 输出高层测试规格
3. 用户审阅总 planner，必要时 `PLANNER_REVISION`
4. 三个阶段分别生成详细 `PhasePlan`
5. 用户逐阶段审阅 plan，必要时修订
6. 三个执行节点生成 SQL
7. 系统合并 SQL
8. 写出 `.json` 和 `.sql`
9. mock executor 验收链路完整性

## Near-Term Direction

下一阶段建议重点关注：

1. 继续收缩低价值字段，保留真正有决策价值的 plan 字段。
2. 把知识检索与证据消费进一步和阶段 plan 对齐。
3. 在不重新写死 prompt 的前提下，增强 CORE 的覆盖展开质量。
4. 再决定是否恢复更强的质量校验，而不是过早用死规则卡住生成链路。
