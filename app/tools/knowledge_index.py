from __future__ import annotations

import json
import traceback
from typing import Any

from agents import function_tool
from agents.run_context import RunContextWrapper

from app.schemas import KnowledgeBundle
from app.services.run_context import SqlMateContext


def _bundle_from_cache(ctx: SqlMateContext, stage: str) -> KnowledgeBundle:
    stage_key = stage.strip().lower() or "shared"
    raw_bundle = ctx.shared_state.setdefault("knowledge_cache", {}).get(stage_key)
    if raw_bundle:
        return KnowledgeBundle.model_validate(raw_bundle)
    return KnowledgeBundle(stage=stage_key)


def _save_bundle(ctx: SqlMateContext, stage: str, bundle: KnowledgeBundle) -> None:
    stage_key = stage.strip().lower() or "shared"
    ctx.shared_state.setdefault("knowledge_cache", {})[stage_key] = bundle.model_dump()


def _merge_into_cache(ctx: SqlMateContext, stage: str, bundle: KnowledgeBundle) -> KnowledgeBundle:
    stage_key = stage.strip().lower() or "shared"
    existing = _bundle_from_cache(ctx, stage_key)
    merged = ctx.knowledge_service.merge_bundles(stage_key, [existing, bundle])
    _save_bundle(ctx, stage_key, merged)
    if stage_key in {"planner", "shared"}:
        shared_existing = _bundle_from_cache(ctx, "shared")
        shared_merged = ctx.knowledge_service.merge_bundles("shared", [shared_existing, bundle])
        _save_bundle(ctx, "shared", shared_merged)
    return merged


@function_tool(
    name_override="retrieve_database_knowledge",
    description_override=(
        "Use Claude Code CLI to retrieve target-database documentation and SQL test examples. "
        "Call this before making target-specific claims about feature behavior, SQL syntax, GUCs, "
        "hints, object definitions, or existing test-case patterns."
    ),
    strict_mode=False,
)
async def retrieve_database_knowledge(
    ctx: RunContextWrapper[SqlMateContext],
    stage: str,
    topics: list[str],
    search_goal: str,
    knowledge_sources: list[str],
    required_evidence: str,
) -> str:
    """Retrieve database-specific knowledge for planning or SQL generation.

    Args:
        stage: One of planner, setup, ddl, core, or shared.
        topics: Specific feature, syntax, GUC, object, or test-case topics to retrieve.
        search_goal: Why this knowledge is needed for the current plan or SQL.
        knowledge_sources: Use docs, sql_examples, or both.
        required_evidence: What evidence the caller needs back, such as feature spec, syntax,
            GUC name/value, usage example, restrictions, or uncertainty notes.
    """
    runtime = ctx.context
    stage_key = stage.strip().lower() or "shared"
    runtime.run_logger.log(
        "knowledge_index_start",
        {
            "stage": stage_key,
            "topics": topics,
            "search_goal": search_goal,
            "knowledge_sources": knowledge_sources,
            "required_evidence": required_evidence,
        },
    )
    if runtime.ui:
        request_preview = {
            "stage": stage_key,
            "topics": topics,
            "search_goal": search_goal,
            "knowledge_sources": knowledge_sources,
            "required_evidence": required_evidence,
        }
        runtime.ui.stream_tool_event(
            "[knowledge request] "
            + json.dumps(request_preview, ensure_ascii=False, indent=2)
        )
    prompt_text = runtime.prompt_loader.load("KNOWLEDGE_INDEX")

    async def debug_callback(channel: str, message: str) -> None:
        if not runtime.ui:
            return
        prefix_map = {
            "status": "[claude status]",
            "stdout": "[claude stdout]",
            "stderr": "[claude stderr]",
        }
        prefix = prefix_map.get(channel, "[claude]")
        text = f"{prefix} {message}"
        if getattr(runtime.ui, "show_think_stream", False):
            runtime.ui.stream_tool_event(text)
        elif channel == "status":
            runtime.ui.stream_tool_event(text)

    try:
        bundle = await runtime.knowledge_service.retrieve_external(
            stage=stage_key,
            topics=topics,
            search_goal=search_goal,
            knowledge_sources=knowledge_sources,
            required_evidence=required_evidence,
            prompt_text=prompt_text,
            debug_callback=debug_callback,
        )
    except Exception as exc:
        runtime.run_logger.log(
            "knowledge_index_error",
            {
                "stage": stage_key,
                "topics": topics,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        if runtime.ui:
            runtime.ui.warning(
                f"[knowledge_index] {stage_key} retrieval failed: "
                f"{type(exc).__name__}: {exc}"
            )
        raise
    if any(item.topic == "claude_raw_result" for item in bundle.found_items):
        warning = (
            f"{stage_key} knowledge retrieval returned raw Claude output, but it did not match "
            "the required structured JSON contract."
        )
        runtime.run_logger.log(
            "knowledge_index_contract_warning",
            {
                "stage": stage_key,
                "message": warning,
                "summary": bundle.distilled_summary,
            },
        )
        if runtime.ui:
            runtime.ui.warning(warning)
    if not bundle.found_items:
        reason_hint = runtime.knowledge_service.summarize_external_issue(bundle.distilled_summary)
        warning = (
            f"{stage_key} knowledge retrieval via Claude failed or returned no evidence; "
            "falling back to configured local document/example scan."
        )
        if reason_hint:
            warning = f"{warning} Cause: {reason_hint}"
        runtime.run_logger.log(
            "knowledge_index_warning",
            {
                "stage": stage_key,
                "message": warning,
                "missing_topics": bundle.missing_topics,
                "claude_summary": bundle.distilled_summary,
            },
        )
        if runtime.ui:
            runtime.ui.warning(warning)
        fallback = runtime.knowledge_service.retrieve_local_fallback(
            stage=stage_key,
            topics=topics,
            search_goal=search_goal,
            knowledge_sources=knowledge_sources,
        )
        if fallback:
            bundle = fallback
    else:
        needs_local_supplement = bool(bundle.missing_topics) or any(
            item.evidence_type == "raw_external_output" for item in bundle.found_items
        )
        if needs_local_supplement:
            fallback = runtime.knowledge_service.retrieve_local_fallback(
                stage=stage_key,
                topics=topics,
                search_goal=search_goal,
                knowledge_sources=knowledge_sources,
            )
            if fallback and fallback.found_items:
                runtime.run_logger.log(
                    "knowledge_index_local_supplement",
                    {
                        "stage": stage_key,
                        "missing_topics_before": bundle.missing_topics,
                        "fallback_found_topics": [item.topic for item in fallback.found_items],
                    },
                )
                bundle = runtime.knowledge_service.merge_bundles(stage_key, [bundle, fallback])
    merged = _merge_into_cache(runtime, stage_key, bundle)
    # ---- 修剪 tool 返回给模型的内容，降低 token 消耗 ----
    # 完整数据已缓存到 shared_state，后续阶段可复用。
    service = runtime.knowledge_service
    max_items = getattr(service, "max_tool_found_items", 5)
    max_chars = getattr(service, "max_tool_item_content_chars", 3000)
    trimmed_items: list[dict[str, Any]] = []
    for item in bundle.found_items[:max_items]:
        d = item.model_dump()
        if len(d.get("content", "")) > max_chars:
            d["content"] = d["content"][:max_chars] + "\n...[trimmed, full content cached]"
        trimmed_items.append(d)
    payload: dict[str, Any] = {
        "stage": stage_key,
        "requested_topics": bundle.requested_topics,
        "found_topics": [item.topic for item in bundle.found_items],
        "missing_topics": bundle.missing_topics,
        "distilled_summary": bundle.distilled_summary,
        "found_items": trimmed_items,
        "cache_status": {
            "cached_topics": [item.topic for item in merged.found_items],
            "missing_topics": merged.missing_topics,
        },
        "usage_rule": (
            "Use found_items as evidence. If missing_topics is not empty, do not claim those "
            "target-specific details are confirmed; carry TODO/Unknown into the plan and add "
            "SQL comments before uncertain generated SQL lines."
            "\n\nCRITICAL: After processing this knowledge, you MUST now output your structured "
            "result as a valid JSON object directly. Do NOT output markdown, analysis, or "
            "explanatory text — output ONLY the JSON object matching your output schema. "
            "Start your response with the '{' character."
        ),
    }
    runtime.run_logger.log(
        "knowledge_index_end",
        {
            "stage": stage_key,
            "found_topics": payload["found_topics"],
            "missing_topics": bundle.missing_topics,
            "summary": bundle.distilled_summary,
        },
    )
    return json.dumps(payload, ensure_ascii=False)


def build_knowledge_tools() -> list[Any]:
    return [retrieve_database_knowledge]
