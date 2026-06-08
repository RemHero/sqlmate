# app/services

## 目录作用

这里放的是跨节点、跨流程复用的公共服务。

## 当前文件说明

- `knowledge.py`
  - 知识检索与多轮知识合并
- `prompt_loader.py`
  - 运行时 prompt 读取
- `providers.py`
  - provider 到 model 的构建逻辑
- `run_context.py`
  - 全局运行时上下文

## 设计原则

1. 与具体业务 prompt 无关
2. 与单个节点职责无关
3. 可被多个流程共同复用

