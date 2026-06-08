# app

## 目录作用

`app/` 是整个项目的核心源码目录，负责：

1. 读取配置
2. 构建运行时依赖
3. 定义节点与结构化模型
4. 编排工作流
5. 管理 CLI 审批交互
6. 记录日志与输出

## 目录结构

- `config.py`
  - 配置模型与配置加载入口
- `main.py`
  - 应用入口
- `logging_setup.py`
  - 日志初始化
- `tracing.py`
  - Agents SDK tracing 配置
- `executors/`
  - SQL 执行器
- `nodes/`
  - 节点抽象与节点实现
- `schemas/`
  - 结构化数据模型
- `services/`
  - 知识、prompt、provider 等公共服务
- `ui/`
  - CLI 界面和审阅交互
- `workflow/`
  - 主流程编排

## 开发建议

1. 与节点无关的公共逻辑优先放入 `services/`
2. 节点间共享的数据结构优先放入 `schemas/`
3. 不要把大段 prompt 写入 Python 代码，统一放入 `node_prompts/`

