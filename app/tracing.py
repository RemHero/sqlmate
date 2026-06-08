from __future__ import annotations

from agents import RunConfig, set_tracing_export_api_key

from app.config import Settings


def build_run_config(settings: Settings, run_id: str) -> RunConfig:
    """根据配置构建 Agents SDK 的运行参数。

    这里会统一设置：
    1. 是否启用 tracing
    2. workflow 名称
    3. trace / group 标识
    4. 元数据与敏感信息开关
    """
    observability = settings.observability
    if observability.enable_sdk_tracing and observability.openai_trace_api_key:
        set_tracing_export_api_key(observability.openai_trace_api_key)

    return RunConfig(
        tracing_disabled=not observability.enable_sdk_tracing,
        workflow_name=observability.workflow_name,
        trace_id=f"trace_{run_id.replace('-', '')[:32]}",
        group_id=run_id,
        trace_metadata={"app": settings.app.name, "run_id": run_id},
        trace_include_sensitive_data=observability.include_sensitive_data,
    )
