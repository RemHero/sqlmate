# app/nodes

## 目录作用

该目录负责封装“节点”这一运行单元。

节点的定义目标是：

1. 统一输入输出方式
2. 隔离具体模型调用细节
3. 为未来扩展更多节点类型留接口

## 当前文件

- `base.py`
  - 节点基类 `BaseNode`
- `llm.py`
  - 基础 LLM 节点
- `agent.py`
  - 带工具能力的 Agent 节点

## 当前两类节点区别

### `LLMNode`

适合：

1. 一次性结构化生成
2. 不需要调用工具
3. 不需要多轮知识获取

典型场景：

- Planner Draft
- Planner Critic

### `AgentNode`

适合：

1. 动态知识检索
2. 多轮推理
3. 用户反馈后的修订

典型场景：

- Planner Finalizer
- Planner Revision
- Phase Refiner
- SETUP / DDL / CORE

