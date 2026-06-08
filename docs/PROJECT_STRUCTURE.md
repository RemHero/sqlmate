# Project Structure

## 1. 项目定位

`SqlMate` 是一个基于 OpenAI Agents SDK 的 SQL 测试用例生成工作流项目。它的核心目标是：

1. 把用户需求转成结构化测试计划
2. 在生成 SQL 之前动态补齐知识证据
3. 将流程拆分为 `SETUP`、`DDL`、`CORE` 三个并行阶段
4. 在关键阶段允许用户参与审阅和修订
5. 对每次运行保留足够详细的日志、结构化结果和可追踪产物

## 2. 目录总览

```text
SqlMate/
  app/                  核心应用源码
  config/               配置模板
  docs/                 项目说明文档
  knowledge/            阶段知识库
  node_prompts/         节点 Prompt 文件
  sample_cases/         样例输入、样例输出、样例脚本
  tests/                测试与 smoke test
  logs/                 运行日志
  output/               主流程输出
  README.md             项目总入口说明
  requirements.txt      Python 依赖列表
```

## 3. `app/` 目录职责

`app/` 是项目的主代码目录，负责真正的运行逻辑。

### 3.1 关键文件

- `app/main.py`
  - 程序入口
  - 负责读取配置、组装依赖、构建节点、执行工作流
- `app/config.py`
  - 配置模型定义
  - 负责 YAML 读取和环境变量展开
- `app/logging_setup.py`
  - 标准日志与 JSONL 事件日志初始化
- `app/tracing.py`
  - Agents SDK 的 tracing 配置

### 3.2 子目录职责

- `app/nodes/`
  - 节点抽象层
  - 区分 `LLMNode` 和 `AgentNode`
- `app/services/`
  - 公共服务层
  - 包括知识检索、prompt 加载、provider 构建、运行时上下文
- `app/workflow/`
  - 编排层
  - 负责 planner 链路、人工审批、阶段执行、最终合并
- `app/ui/`
  - CLI 交互界面
  - 负责美化展示、审批、反馈采集
- `app/executors/`
  - SQL 执行器抽象
  - 当前仅包含 mock executor
- `app/schemas/`
  - 所有结构化输入输出模型

## 4. `knowledge/` 目录职责

该目录存放当前版本的本地知识库。每个阶段一个子目录：

- `knowledge/setup/`
- `knowledge/ddl/`
- `knowledge/core/`

每个目录下当前使用 `base_knowledge.json` 作为最基础的 KV Store。后续可以扩展为：

1. 多文件知识源
2. 版本化知识目录
3. 远程检索接口
4. 向量库或索引层

## 5. `node_prompts/` 目录职责

这里保存所有节点的 prompt 文件，运行时按节点名动态加载。

### 当前节点类型

- Planner 主链路节点
  - `PLANNER_DRAFT.md`
  - `PLANNER_CRITIC.md`
  - `PLANNER_FINALIZER.md`
  - `PLANNER_REVISION.md`
- 子计划修订节点
  - `SETUP_PLAN_REFINER.md`
  - `DDL_PLAN_REFINER.md`
  - `CORE_PLAN_REFINER.md`
- SQL 生成节点
  - `SETUP.md`
  - `DDL.md`
  - `CORE.md`
- 预留但当前主流程未接入的节点
  - `SQL_REPAIR.md`

说明：

1. 当前仓库中的旧兼容占位 prompt 已清理。
2. `PLANNER_CRITIC.md` 和 `PLANNER_FINALIZER.md` 仍有代码绑定，但默认 fast path 不执行。
3. `SQL_REPAIR.md` 当前只保留文件与配置绑定，主流程尚未接入。

## 6. `sample_cases/` 目录职责

用于保存可重复演示的样例资产。

- `inputs/`
  - 样例输入 JSON
- `outputs/`
  - 样例流程生成结果
- `transcripts/`
  - 样例运行索引信息
- `run_mock_samples.py`
  - 批量跑样例的脚本

当前样例使用 mock pipeline，目的是先验证“流程、审批、输出落盘”是否正常。

## 7. `tests/` 目录职责

这里主要放低成本 smoke test。

- `tests/smoke_workflow.py`
  - 用假 planner 和假阶段节点验证：
    - 编排逻辑
    - 审批回路
    - 输出落盘
    - JSON 结构完整性

## 8. `logs/` 与 `output/`

- `logs/`
  - 保存 `.log` 和 `.jsonl`
  - `.jsonl` 里是逐事件明细
- `output/`
  - 保存真实主流程的最终 JSON 输出

## 9. 目录之间的依赖关系

整体依赖方向如下：

1. `main.py` 读取 `config/`
2. `main.py` 组装 `services/`、`nodes/`、`workflow/`、`ui/`
3. `workflow/` 调用 `nodes/`
4. `nodes/` 运行时读取 `node_prompts/`
5. `AgentNode` 调用 `KnowledgeService` 读取 `knowledge/`
6. 结果和日志落到 `output/`、`logs/`
7. `sample_cases/` 和 `tests/` 用于验证主流程

## 10. 后续扩展建议

### 10.1 代码层

1. 为真实数据库执行器建立统一接口
2. 为 repair loop 建立专门的阶段路由器
3. 将 planner 质疑逻辑升级为多模型并行裁决
4. 增加更细粒度的运行指标采集

### 10.2 文档层

1. 给每个 prompt 补充真实业务约束
2. 为知识库建立命名规范和维护规范
3. 为样例输入建立分类说明

### 10.3 工具链层

1. 接入真实数据库环境
2. 引入持续集成脚本
3. 建立标准回归样例集
