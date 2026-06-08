# knowledge

## 目录作用

保存阶段化知识库。

当前按阶段拆分：

- `setup/`
- `ddl/`
- `core/`

每个阶段目前用一个 `base_knowledge.json` 表示本地 KV Store。

## 当前作用

1. 给 planner 预热基础 evidence
2. 给 phase agent 提供运行时动态取证能力

## 后续扩展方向

1. 引入多文件知识源
2. 引入数据库版本维度
3. 引入远程检索服务

