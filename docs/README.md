# docs

## 目录作用

保存项目的说明性文档。

## 当前文档

### 项目概览

- `SQLMATE_PROJECT_SUMMARY.md`
  - 项目全面总览：核心功能、设计动机、技术细节、演进历程
- `MID_PROJECT_ARCHITECTURE.md`
  - 中期架构概述：设计哲学、工作流步骤、数据契约、prompt 策略

### 工作流与运行时

- `WORKFLOW_OVERVIEW.md`
  - 工作流总览（两阶段规划 + 三阶段生成）
- `WORKFLOW_RUNTIME.md`
  - 详细运行时说明：初始化、planner、子计划审阅、阶段执行、
    知识取证、SQL 合并、错误重试与 JSON 修复、checkpoint/resume、日志结构
- `INTERACTIVE_REVIEW.md`
  - 交互式审阅说明：审阅节点、反馈回路、checkpoint 持久化、`--resume` 用法

### 设计细节

- `DESIGN_DETAILS.md`
  - 关键架构决策的设计理由
- `PROJECT_STRUCTURE.md`
  - 项目目录结构详细说明（含 checkpoint 和新的日志目录结构）

### 知识系统

- `KNOWLEDGE_INDEX.md`
  - 知识索引能力（Claude Code CLI + fallback）详解
- `DYNAMIC_KNOWLEDGE.md`
  - 动态知识检索的运行时行为说明

### 参考

- `KNOWN_ISSUES.md`
  - 已知问题与未来设计（知识检索时机等）
- `MODEL_CAPABILITIES_TODO.md`
  - 模型能力配置化改造 TODO（消除硬编码分支）
- `PROMPT_FILLING_GUIDE.md`
  - Prompt 填写指引
- `SAMPLE_CASES.md`
  - 样例输入/输出说明

## 维护建议

当目录结构、主流程或核心设计变化时，应优先同步更新本目录文档。
