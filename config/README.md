# config

## 目录作用

保存项目配置模板。

## 当前文件说明

- `settings.example.yaml`
  - 应用路径
  - tracing 配置
  - planner 配置
  - knowledge 配置
  - executor 配置
  - UI 配置
  - providers
  - node 到 provider 的绑定关系

## 维护建议

1. 新增节点时同步补充 `node_bindings`
2. 新增模型提供方时补充 `providers`
3. 尽量把敏感值放环境变量，不要直接写明文

