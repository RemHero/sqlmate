from __future__ import annotations

import json
from typing import Any

from agents import Agent, AgentOutputSchema, ModelSettings, Runner
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from agents.stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent
from openai.types.responses.response_reasoning_summary_text_delta_event import (
    ResponseReasoningSummaryTextDeltaEvent,
)
from openai.types.responses.response_reasoning_text_delta_event import ResponseReasoningTextDeltaEvent
from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from app.nodes.base import BaseNode
from app.schemas import KnowledgeBundle
from app.services.providers import set_current_json_schema_support
from app.services.run_context import SqlMateContext


def _bundle_from_cache(ctx: SqlMateContext, stage: str) -> KnowledgeBundle:
    cache = ctx.shared_state.setdefault("knowledge_cache", {})
    stage_key = stage.lower()
    raw_bundle = cache.get(stage_key)
    if raw_bundle:
        return KnowledgeBundle.model_validate(raw_bundle)
    return KnowledgeBundle(stage=stage_key)


def _infer_stage(node_name: str, payload: dict[str, Any]) -> str | None:
    if node_name.startswith("PLANNER"):
        return "planner"
    if "phase_plan" in payload and isinstance(payload["phase_plan"], dict):
        stage = str(payload["phase_plan"].get("stage", "")).strip().lower()
        if stage:
            return stage
    if "phase_outline" in payload and isinstance(payload["phase_outline"], dict):
        stage = str(payload["phase_outline"].get("stage", "")).strip().lower()
        if stage:
            return stage
    prefix = node_name.split("_", 1)[0].lower()
    if prefix in {"setup", "ddl", "core"}:
        return prefix
    return None


def _collect_requested_topics(payload: dict[str, Any]) -> list[str]:
    topics: list[str] = []
    if isinstance(payload.get("phase_plan"), dict):
        topics.extend(str(item) for item in payload["phase_plan"].get("required_topics", []) if str(item).strip())
    if isinstance(payload.get("current_phase_plan"), dict):
        topics.extend(str(item) for item in payload["current_phase_plan"].get("required_topics", []) if str(item).strip())
    return topics


def _enrich_payload_with_knowledge(
    ctx: SqlMateContext,
    node_name: str,
    payload: dict[str, Any],
    *,
    knowledge_tools_available: bool,
) -> dict[str, Any]:
    stage = _infer_stage(node_name, payload)
    if not stage:
        return payload
    requested_topics = _collect_requested_topics(payload)
    existing = _bundle_from_cache(ctx, stage)
    if requested_topics:
        retrieved = ctx.knowledge_service.retrieve(stage, requested_topics, reason=f"{node_name.lower()} preload")
        merged = ctx.knowledge_service.merge_bundles(stage, [existing, retrieved])
    else:
        merged = existing
    ctx.shared_state.setdefault("knowledge_cache", {})[stage] = merged.model_dump()
    enriched = dict(payload)
    enriched["cached_knowledge"] = merged.model_dump()
    shared = _bundle_from_cache(ctx, "shared")
    if stage not in {"shared", "planner"}:
        enriched["shared_cached_knowledge"] = shared.model_dump()
    enriched["available_knowledge_topics"] = ctx.knowledge_service.list_topics(stage)
    enriched["knowledge_instruction"] = {
        **(payload.get("knowledge_instruction") or {}),
        "knowledge_tools_available": knowledge_tools_available,
        "knowledge_tool_name": "retrieve_database_knowledge" if knowledge_tools_available else "",
        "fallback_rule": "如果资料或用例检索不到，在 knowledge_gaps/evidence_requirements/evidence_checklist 中写 TODO，不要编造；最终 SQL 中对应不确定语法行前必须加 TODO 证据缺失注释。",
    }
    return enriched


def _strip_markdown_fences(text: str) -> str:
    """去掉模型输出外的 markdown 代码块包裹（```json ... ```）。"""
    t = text.strip()
    if t.startswith("```"):
        newline_idx = t.find("\n")
        if newline_idx != -1:
            t = t[newline_idx + 1:]
        else:
            t = t[3:]
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3].strip()
    return t




class AgentNode(BaseNode):
    """具备工具调用能力的节点封装。

    根据模型名称自动适配 thinking / tool_choice 策略：

    - DeepSeek：thinking 通过 extra_body={"thinking":{"type":"enabled"}} 开启，
      tool_choice=None（thinking 模式不支持显式 tool_choice），执行后检查
      工具是否被调用，未调用则强制检索后重新生成。

    - 豆包 / 其他模型：reasoning_effort="max" 开启 thinking，
      tool_choice="required" 强制首轮调用工具。

    - JSON 格式容错：模型输出 JSON 格式不符合 schema 时，
      自动走多策略恢复（去 markdown 壳 → flatten dict → 占位对象），
      永不因格式问题崩溃。极端情况下回退到两阶段模式。
    """

    def __init__(
        self,
        node_name: str,
        model,
        output_type,
        run_config,
        max_turns: int = 10,
        tools: list[Any] | None = None,
        force_first_tool: bool = False,
        model_extra_body: dict | None = None,
        max_output_tokens: int = 16384,
        max_tool_rounds: int = 1,
        supports_json_schema: bool = False,
    ) -> None:
        super().__init__(node_name)
        self.model = model
        self.output_type = output_type
        self.run_config = run_config
        self.max_turns = max_turns
        self.tools = tools or []
        self.force_first_tool = force_first_tool
        self.model_extra_body = model_extra_body
        self.max_output_tokens = max_output_tokens
        self.max_tool_rounds = max_tool_rounds
        self.supports_json_schema = supports_json_schema

        model_name = self._model_name()
        self._is_thinking_model = "deepseek" in model_name or "glm" in model_name

    def _model_name(self) -> str:
        """获取当前模型名称字符串（如 deepseek-v4-pro）。"""
        return getattr(self.model, "model", "").lower()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def run(
        self,
        ctx: SqlMateContext,
        payload: dict[str, Any],
        previous_response_id: str | None = None,
    ) -> Any:
        if ctx.ui:
            await ctx.ui.maybe_pause()
            ctx.ui.raise_if_interrupted()

        prompt = ctx.prompt_loader.load(self.node_name)
        if self.output_type is not None and self.output_type is not str:
            schema_desc = json.dumps(self.output_type.model_json_schema(), ensure_ascii=False, indent=2)
            prompt = (
                prompt
                + "\n\n=== OUTPUT FORMAT -- CRITICAL ==="
                + "\nYou MUST output ONLY a single JSON object. Your entire response must be the JSON object itself, nothing else."
                + "\nDo NOT write any markdown, analysis, explanation, reasoning, or text before or after the JSON."
                + "\nThe JSON must exactly match this schema:"
                + "\n" + schema_desc
            )
            if self._is_thinking_model:
                prompt = (
                    prompt
                    + "\n\n=== IMPORTANT: THINKING vs FINAL OUTPUT ==="
                    + "\nYour thinking/reasoning phase is separate from your final output."
                    + "\nAfter the thinking phase ends, your final content must be ONLY the JSON object matching the schema above."
                    + "\nDo NOT output analysis, summaries, or any other text as the final content."
                )

        # ---- 注入工具调用轮次限制 ----
        if self.force_first_tool and self.tools and self.max_tool_rounds > 0:
            tool_limit_msg = (
                f"\n\n【工具调用限制】你最多只能进行 {self.max_tool_rounds} 轮知识检索"
                f"（即最多调用 {self.max_tool_rounds} 次 retrieve_database_knowledge 工具）。"
                f"请在调用工具前规划好检索策略，一次性检索所有必要主题，避免重复调用浪费轮次。"
            )
            prompt = prompt + tool_limit_msg

        # ---- 根据模型名构建 extra_body ----
        model_name = self._model_name()
        is_deepseek = "deepseek" in model_name
        is_doubao = "doubao" in model_name or "glm" in model_name

        extra_body = dict(self.model_extra_body or {})
        if is_deepseek:
            # DeepSeek 官方文档：thinking 通过 extra_body 开启
            extra_body.setdefault("thinking", {"type": "enabled"})
        # reasoning_effort 仅对支持该参数的非豆包模型设置
        if not is_doubao:
            extra_body.setdefault("reasoning_effort", "max")

        enriched_payload = _enrich_payload_with_knowledge(
            ctx, self.node_name, payload,
            knowledge_tools_available=bool(self.tools),
        )

        # ---- 决定 tool_choice ----
        if is_deepseek:
            # DeepSeek thinking 模式不支持显式 tool_choice 参数
            tool_choice = None
        elif self.force_first_tool and self.tools:
            # 豆包等标准模型：tool_choice="required" 正常工作
            tool_choice = "required"
        else:
            tool_choice = None

        # ---- 限制工具调用轮次 ----
        # DeepSeek 路径 tool_choice=None，模型可能反复调工具耗尽 turns。
        # 用 max_tool_rounds 限制最多调几次工具就强制输出。
        effective_max_turns: int | None = None
        if is_deepseek and self.force_first_tool and self.tools:
            effective_max_turns = self.max_tool_rounds * 2 + 2

        # ---- 执行（含一次 JSON 解析失败重试）----
        # B（SDK JSON 校验）失败抛 ModelBehaviorError 时，重试 A（_single_agent_run）一次，
        # 两次都失败则走 _fallback_two_phase 兜底。
        MAX_JSON_RETRIES = 1
        for attempt in range(MAX_JSON_RETRIES + 1):
            try:
                output, last_response_id, tool_called = await self._single_agent_run(
                    ctx, prompt, enriched_payload, previous_response_id,
                    tool_choice=tool_choice, extra_body=extra_body,
                    max_turns=effective_max_turns,
                )
                break  # 成功
            except ModelBehaviorError as exc:
                error_text = str(exc)[:500]
                if attempt < MAX_JSON_RETRIES:
                    ctx.run_logger.log("json_parse_retry", {
                        "node": self.node_name,
                        "attempt": attempt + 1,
                        "retries_allowed": MAX_JSON_RETRIES,
                        "error": error_text,
                    })
                    if ctx.ui:
                        ctx.ui.warning(
                            f"[{self.node_name}] JSON 解析失败（attempt {attempt + 1}/{MAX_JSON_RETRIES + 1}），"
                            f"正在重试模型调用...\n  {error_text}"
                        )
                else:
                    return await self._fallback_two_phase(
                        ctx, prompt, payload, previous_response_id, extra_body, exc
                    )

        # ---- DeepSeek：检查模型是否调用了工具 ----
        if is_deepseek and self.force_first_tool and self.tools and not tool_called:
            if ctx.ui:
                ctx.ui.warning(
                    f"[{self.node_name}] DeepSeek did not call any tools. "
                    "Forcing knowledge retrieval before regeneration."
                )
            ctx.run_logger.log("force_knowledge_retrieval", {
                "node": self.node_name,
                "reason": "deepseek_did_not_call_tools",
            })
            await self._force_knowledge_retrieval(ctx, prompt, payload)

            enriched_payload = _enrich_payload_with_knowledge(
                ctx, self.node_name, payload,
                knowledge_tools_available=bool(self.tools),
            )
            for attempt in range(MAX_JSON_RETRIES + 1):
                try:
                    output, last_response_id, _ = await self._single_agent_run(
                        ctx, prompt, enriched_payload, previous_response_id,
                        tool_choice=None, extra_body=extra_body,
                        max_turns=effective_max_turns,
                    )
                    break  # 成功
                except ModelBehaviorError as exc:
                    error_text = str(exc)[:500]
                    if attempt < MAX_JSON_RETRIES:
                        ctx.run_logger.log("json_parse_retry", {
                            "node": self.node_name,
                            "attempt": attempt + 1,
                            "retries_allowed": MAX_JSON_RETRIES,
                            "error": error_text,
                        })
                        if ctx.ui:
                            ctx.ui.warning(
                                f"[{self.node_name}] JSON 解析失败（attempt {attempt + 1}/{MAX_JSON_RETRIES + 1}），"
                                f"正在重试模型调用...\n  {error_text}"
                            )
                    else:
                        return await self._fallback_two_phase(
                            ctx, prompt, payload, previous_response_id, extra_body, exc
                        )

        return output, last_response_id

    # ------------------------------------------------------------------
    # 单 Agent 执行（核心流式逻辑）
    # ------------------------------------------------------------------

    async def _single_agent_run(
        self,
        ctx: SqlMateContext,
        prompt: str,
        enriched_payload: dict[str, Any],
        previous_response_id: str | None,
        *,
        tool_choice: str | None,
        extra_body: dict | None,
        max_turns: int | None = None,
    ) -> tuple[Any, str | None, bool]:
        """标准单 Agent 流式执行。

        Args:
            max_turns: 覆盖 self.max_turns。用于限制工具调用轮次，
                       例如 max_tool_rounds=1 时传入 max_turns=3。

        Returns:
            (output, last_response_id, tool_called)
        """
        set_current_json_schema_support(self.supports_json_schema)

        agent = Agent(
            name=self.node_name,
            instructions=prompt,
            model=self.model,
            tools=self.tools,
            model_settings=ModelSettings(
                tool_choice=tool_choice,
                extra_body=extra_body,
                max_output_tokens=self.max_output_tokens,
            ),
            output_type=AgentOutputSchema(self.output_type, strict_json_schema=False),
        )
        tool_names = [getattr(tool, "name", str(tool)) for tool in self.tools]
        ctx.run_logger.log(
            "node_start",
            {
                "node": self.node_name,
                "payload": enriched_payload,
                "previous_response_id": previous_response_id,
                "tools": tool_names,
                "tool_choice": tool_choice,
                "force_first_tool": self.force_first_tool,
                "extra_body_keys": list(extra_body.keys()) if extra_body else [],
            },
        )
        result = Runner.run_streamed(
            agent,
            input=json.dumps(enriched_payload, ensure_ascii=False),
            context=ctx,
            max_turns=max_turns if max_turns is not None else self.max_turns,
            run_config=self.run_config,
            previous_response_id=previous_response_id,
        )
        reasoning_chunks: list[str] = []
        text_chunks: list[str] = []
        tool_called = False
        output = None
        if ctx.ui:
            ctx.ui.stream_begin(self.node_name)
        try:
            # B（SDK 内置 JSON 校验）在 stream_events 或 final_output 阶段可能抛出
            # ModelBehaviorError。不在此处内部恢复——让异常向上传播到 run()，
            # 由 run() 的重试逻辑决定重试 A（_single_agent_run）或走 _fallback_two_phase。
            async for event in result.stream_events():
                if isinstance(event, RawResponsesStreamEvent):
                    data = event.data
                    if isinstance(data, (ResponseReasoningSummaryTextDeltaEvent, ResponseReasoningTextDeltaEvent)):
                        reasoning_chunks.append(data.delta)
                        if ctx.ui:
                            ctx.ui.stream_reasoning_delta(data.delta)
                    elif isinstance(data, ResponseTextDeltaEvent):
                        text_chunks.append(data.delta)
                        if ctx.ui:
                            ctx.ui.stream_text_delta(data.delta)
                elif isinstance(event, RunItemStreamEvent):
                    if event.item.type == "tool_call_item":
                        tool_called = True
                    if ctx.ui:
                        if event.item.type == "tool_call_item":
                            ctx.ui.stream_tool_event("[tool called]")
                        elif event.item.type == "tool_call_output_item":
                            ctx.ui.stream_tool_output(str(event.item.output))
                elif isinstance(event, AgentUpdatedStreamEvent):
                    if ctx.ui:
                        ctx.ui.stream_tool_event(f"[agent updated] {event.new_agent.name}")
            raw_text = "".join(text_chunks)
            try:
                output = result.final_output
            except ModelBehaviorError:
                output = self._try_heal(ctx, raw_text)
                if output is None:
                    raise

            if ctx.ui:
                rendered_output = (
                    output.model_dump_json(indent=2)
                    if hasattr(output, "model_dump_json")
                    else json.dumps(output, ensure_ascii=False, indent=2)
                    if isinstance(output, dict)
                    else str(output)
                )
                ctx.ui.stream_final_block(f"{self.node_name} Final", rendered_output)
        finally:
            if ctx.ui:
                ctx.ui.stream_end()
        if ctx.ui:
            ctx.ui.raise_if_interrupted()
        ctx.run_logger.log(
            "node_end",
            {
                "node": self.node_name,
                "output": output.model_dump() if hasattr(output, "model_dump") else str(output),
                "last_response_id": getattr(result, "last_response_id", None),
                "tool_called": tool_called,
            },
        )
        if reasoning_chunks:
            ctx.run_logger.log(
                "node_reasoning",
                {
                    "node": self.node_name,
                    "reasoning": "".join(reasoning_chunks),
                },
            )
        return output, getattr(result, "last_response_id", None), tool_called

    # ------------------------------------------------------------------
    # JSON 修复：SDK 解析失败时，用原始文本做多策略恢复
    # ------------------------------------------------------------------

    def _try_heal(self, ctx: SqlMateContext, raw_text: str) -> Any:
        """用原始文本尝试恢复结构化输出，成功返回对象，失败返回 None。

        策略 1: 去掉 markdown 代码块包裹后 model_validate_json
        策略 2: json.loads + model_validate
        """
        cleaned = _strip_markdown_fences(raw_text)
        if cleaned != raw_text:
            ctx.run_logger.log("try_heal_s1_markdown_strip", {"node": self.node_name})

        # 策略 1
        try:
            result = self.output_type.model_validate_json(cleaned)
            ctx.run_logger.log("try_heal_s1_ok", {"node": self.node_name})
            if ctx.ui:
                ctx.ui.warning(f"[{self.node_name}] JSON 格式自动修复成功（策略1: 去 markdown 壳）")
            return result
        except Exception:
            pass

        # 策略 2
        try:
            data = json.loads(cleaned)
            result = self.output_type.model_validate(data)
            ctx.run_logger.log("try_heal_s2_ok", {"node": self.node_name})
            if ctx.ui:
                ctx.ui.warning(f"[{self.node_name}] JSON 格式自动修复成功（策略2: json.loads + validate）")
            return result
        except Exception:
            pass

        ctx.run_logger.log("try_heal_failed", {
            "node": self.node_name,
            "raw_text_preview": raw_text[:500],
        })
        return None

    # ------------------------------------------------------------------
    # 强制知识检索
    # ------------------------------------------------------------------

    async def _force_knowledge_retrieval(
        self,
        ctx: SqlMateContext,
        prompt: str,
        payload: dict[str, Any],
    ) -> None:
        """模型未主动调用工具时的兜底检索。

        不设 thinking，使用 tool_choice="required" 强制调用知识检索工具，
        结果写入 ctx.shared_state 缓存。
        """
        retrieval_payload = _enrich_payload_with_knowledge(
            ctx, self.node_name, payload,
            knowledge_tools_available=True,
        )
        try:
            await self._phase_retrieve_knowledge(ctx, prompt, retrieval_payload)
        except MaxTurnsExceeded:
            # 工具已在 Turn 1 成功执行并写入了 ctx.shared_state 缓存；
            # tool_choice="required" 导致 Turn 2 再次强制调用工具后超限，
            # 但缓存的副作用已生效，忽略此错误。
            if ctx.ui:
                ctx.ui.warning(
                    f"[{self.node_name}] Knowledge retrieval exceeded max turns, "
                    "but tool results are already cached."
                )

    # ------------------------------------------------------------------
    # 两阶段回退模式（处理 ModelBehaviorError / JSON 格式异常）
    # ------------------------------------------------------------------

    async def _fallback_two_phase(
        self,
        ctx: SqlMateContext,
        prompt: str,
        payload: dict[str, Any],
        previous_response_id: str | None,
        extra_body: dict | None,
        original_error: BaseException | None = None,
    ) -> Any:
        """主策略遇到 ModelBehaviorError 时的两阶段回退。

        阶段一：强制知识检索（不设 thinking，tool_choice="required"）。
        阶段二：重新生成（thinking 开启，知识已就绪）。
        """
        error_text = str(original_error) if original_error else "unknown"
        log_msg = (
            f"[{self.node_name}] {error_text}\n"
            "Falling back to two-phase approach:\n"
            "  Phase 1 (no thinking, tool_choice=required) → knowledge retrieval\n"
            "  Phase 2 (thinking enabled, knowledge from cache) → plan generation"
        )
        if ctx.ui:
            ctx.ui.warning(log_msg)
        ctx.run_logger.log(
            "two_phase_fallback",
            {
                "node": self.node_name,
                "reason": "model_behavior_error_json_invalid",
                "fallback_strategy": "two_phase",
                "original_error": error_text,
                "exception_type": type(original_error).__name__ if original_error else "unknown",
            },
        )

        # ---- 阶段一：知识检索 ----
        await self._force_knowledge_retrieval(ctx, prompt, payload)
        if ctx.ui:
            ctx.ui.raise_if_interrupted()

        # ---- 阶段二：生成计划 ----
        enriched_payload = _enrich_payload_with_knowledge(
            ctx, self.node_name, payload,
            knowledge_tools_available=bool(self.tools),
        )
        output, last_response_id, _ = await self._single_agent_run(
            ctx, prompt, enriched_payload, previous_response_id,
            tool_choice=None, extra_body=extra_body,
        )
        return output, last_response_id

    # ------------------------------------------------------------------
    # 低级知识检索
    # ------------------------------------------------------------------

    async def _phase_retrieve_knowledge(
        self,
        ctx: SqlMateContext,
        prompt: str,
        retrieval_payload: dict[str, Any],
    ) -> None:
        """强制调用知识检索工具，不生成最终计划。

        DeepSeek v4-pro 默认开启 thinking，而 thinking + tool_choice="required"
        会导致 400 错误，因此必须显式关闭 thinking。
        豆包/GLM 等模型不需要 thinking 参数，设为 None 即可。
        """
        is_deepseek = "deepseek" in self._model_name()
        retrieval_extra_body = {"thinking": {"type": "disabled"}} if is_deepseek else None

        retrieval_agent = Agent(
            name=f"{self.node_name}_RETRIEVAL",
            instructions=prompt,
            model=self.model,
            tools=self.tools,
            model_settings=ModelSettings(
                tool_choice="required",
                extra_body=retrieval_extra_body,
            ),
        )
        ctx.run_logger.log(
            "node_start",
            {
                "node": f"{self.node_name}_RETRIEVAL",
                "phase": "knowledge_retrieval",
                "payload": retrieval_payload,
                "tools": [getattr(tool, "name", str(tool)) for tool in self.tools],
            },
        )
        result = Runner.run_streamed(
            retrieval_agent,
            input=json.dumps(retrieval_payload, ensure_ascii=False),
            context=ctx,
            max_turns=2,
            run_config=self.run_config,
        )
        if ctx.ui:
            ctx.ui.stream_begin(f"{self.node_name} Knowledge Retrieval")
        try:
            async for event in result.stream_events():
                if isinstance(event, RunItemStreamEvent):
                    if ctx.ui:
                        if event.item.type == "tool_call_item":
                            ctx.ui.stream_tool_event("[知识检索中...]")
                        elif event.item.type == "tool_call_output_item":
                            ctx.ui.stream_tool_output(str(event.item.output))
        finally:
            if ctx.ui:
                ctx.ui.stream_end()
        ctx.run_logger.log(
            "node_end",
            {
                "node": f"{self.node_name}_RETRIEVAL",
                "phase": "knowledge_retrieval_complete",
            },
        )
