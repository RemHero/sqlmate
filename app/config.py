from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# 用于解析 `${ENV}` 或 `${ENV:-default}` 形式的环境变量占位符。
ENV_PATTERN = re.compile(r"\$\{([^}:]+)(:-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    """递归展开配置对象中的环境变量占位符。

    这个函数的作用是让 `settings.example.yaml` 中的敏感信息和环境差异项
    可以延迟到运行时再注入，例如 API Key、Base URL 或默认值。
    """
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        default = match.group(3) or ""
        return os.environ.get(key, default)

    prev = value
    while True:
        current = ENV_PATTERN.sub(repl, prev)
        if current == prev:
            return current
        prev = current


class AppPaths(BaseModel):
    """定义项目运行时会使用到的核心目录。"""

    name: str = "sqlmate"
    prompts_dir: str = "node_prompts"
    knowledge_dir: str = "knowledge"
    log_dir: str = "logs"
    output_dir: str = "output"


class ObservabilityConfig(BaseModel):
    """定义日志、Tracing 等可观测性行为。"""

    workflow_name: str = "SqlMate Workflow"
    enable_sdk_tracing: bool = False
    openai_trace_api_key: str = ""
    include_sensitive_data: bool = False


class PlannerConfig(BaseModel):
    """定义 planner 阶段的轮数和单节点最大对话轮次。"""

    debate_rounds: int = 2
    max_turns: int = 12
    max_output_tokens: int = 16384
    max_tool_rounds: int = 1


class KnowledgeConfig(BaseModel):
    """定义知识检索能力的匹配与多轮行为参数。"""

    max_matches_per_topic: int = 3
    max_tool_rounds: int = 6
    enable_claude_index: bool = True
    claude_command: str = "claude"
    claude_permission_mode: str = "bypassPermissions"
    claude_use_add_dir: bool = True
    claude_timeout_seconds: int = 600
    claude_max_output_chars: int = 12000
    doc_knowledge_dir: str = "/home/remhero/shared/ai/SqlMate/worflow/txzn"
    doc_knowledge_dirs: list[str] = Field(default_factory=list)
    sql_examples_dir: str = "knowledge/sql_examples_empty"
    max_tool_found_items: int = 5
    max_tool_item_content_chars: int = 3000


class ExecutorConfig(BaseModel):
    """定义 SQL 执行器的运行方式。"""

    type: str = "mock"
    max_repair_rounds: int = 0
    phase_timeout_seconds: int = 120
    allow_phase_fallback: bool = True


class UIConfig(BaseModel):
    """定义 CLI 界面的显示与审批行为。"""

    approval_mode: str = "interactive"
    show_banner: bool = True
    show_raw_json: bool = False
    show_think_stream: bool = False


class ProviderConfig(BaseModel):
    """定义单个模型提供方的访问方式。

    一个 provider 对应一套 base_url、api_key、model 配置，
    供不同节点按需绑定。
    """

    base_url: str
    api_key: str
    model: str
    use_responses: bool = True
    timeout_seconds: float = 180.0
    max_retries: int = 2
    organization: str | None = None
    project: str | None = None
    model_extra_body: dict = {}
    supports_json_schema: bool = False


class NodeBinding(BaseModel):
    """定义节点与 provider 的绑定关系。"""

    provider: str
    max_turns: int | None = None


class Settings(BaseModel):
    """整个应用的总配置模型。"""

    app: AppPaths = Field(default_factory=AppPaths)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    providers: dict[str, ProviderConfig]
    node_bindings: dict[str, NodeBinding]


def load_settings(config_path: str | Path) -> Settings:
    """从 YAML 文件加载配置，并完成环境变量展开与模型校验。"""
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    expanded = _expand_env(raw)
    settings = Settings.model_validate(expanded)
    return settings
