# app/executors

## 目录作用

这里存放 SQL 执行器相关实现。

## 当前文件说明

- `mock_sql_executor.py`
  - 本地 smoke test 用的假执行器

## 后续扩展方向

1. 真实数据库执行器
2. 带 repair loop 的执行器
3. 根据错误路由到具体阶段的执行器

