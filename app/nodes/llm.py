from __future__ import annotations

import json
from typing import Any

from agents import Agent, AgentOutputSchema, ModelSettings, Runner
from agents.stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent
from openai.types.responses.response_reasoning_summary_text_delta_event import (
    ResponseReasoningSummaryTextDeltaEvent,
)
from openai.types.responses.response_reasoning_text_delta_event import ResponseReasoningTextDeltaEvent
from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from app.nodes.base import BaseNode
from app.services.run_context import SqlMateContext


class LLMNode(BaseNode):
    """轻量级结构化节点封装。

    与 `AgentNode` 相比，它不绑定工具，适合一次性生成：
    例如 planner draft、critic 这类偏纯生成节点。
    """

    def __init__(
        self,
        node_name: str,
        model,
        output_type,
        run_config,
        max_turns: int = 8,
        model_extra_body: dict | None = None,
        enable_thinking: bool = False,
    ) -> None:
        super().__init__(node_name)
        self.model = model
        self.output_type = output_type
        self.run_config = run_config
        self.max_turns = max_turns
        self.model_extra_body = model_extra_body
        self.enable_thinking = enable_thinking

    async def run(self, ctx: SqlMateContext, payload: dict[str, Any]) -> Any:
        """执行一次基础 LLM 节点调用并返回结构化结果。"""
        if ctx.ui:
            await ctx.ui.maybe_pause()
            ctx.ui.raise_if_interrupted()
        prompt = ctx.prompt_loader.load(self.node_name)
        if self.output_type is not None and self.output_type is not str:
            schema_desc = json.dumps(self.output_type.model_json_schema(), ensure_ascii=False, indent=2)
            prompt = (
                prompt
                + "\n\n=== OUTPUT FORMAT -- CRITICAL ==="
                + "\nYou MUST output ONLY a single JSON object. Start your response with the '{' character."
                + "\nDo NOT write any markdown, analysis, explanation, reasoning, or text before or after the JSON."
                + "\nThe JSON must exactly match this schema:"
                + "\n" + schema_desc
            )
        extra_body = dict(self.model_extra_body or {})
        model_name = getattr(self.model, "model", "").lower()
        if "glm" in model_name:
            extra_body.setdefault("chat_template_kwargs", {}).setdefault(
                "enable_thinking", self.enable_thinking
            )

        agent = Agent(
            name=self.node_name,
            instructions=prompt,
            model=self.model,
            model_settings=ModelSettings(extra_body=extra_body or None),
            output_type=AgentOutputSchema(self.output_type, strict_json_schema=False),
        )
        ctx.run_logger.log("node_start", {"node": self.node_name, "payload": payload})
        result = Runner.run_streamed(
            agent,
            input=json.dumps(payload, ensure_ascii=False),
            context=ctx,
            max_turns=self.max_turns,
            run_config=self.run_config,
        )
        reasoning_chunks: list[str] = []
        if ctx.ui:
            ctx.ui.stream_begin(self.node_name)
        async for event in result.stream_events():
            if isinstance(event, RawResponsesStreamEvent):
                data = event.data
                if isinstance(data, (ResponseReasoningSummaryTextDeltaEvent, ResponseReasoningTextDeltaEvent)):
                    reasoning_chunks.append(data.delta)
                    if ctx.ui:
                        ctx.ui.stream_reasoning_delta(data.delta)
                elif isinstance(data, ResponseTextDeltaEvent):
                    if ctx.ui:
                        ctx.ui.stream_text_delta(data.delta)
            elif isinstance(event, RunItemStreamEvent):
                if ctx.ui:
                    if event.item.type == "tool_call_item":
                        ctx.ui.stream_tool_event("[tool called]")
                    elif event.item.type == "tool_call_output_item":
                        ctx.ui.stream_tool_output(str(event.item.output))
            elif isinstance(event, AgentUpdatedStreamEvent) and ctx.ui:
                ctx.ui.stream_tool_event(f"[agent updated] {event.new_agent.name}")
        output = result.final_output
        if ctx.ui:
            rendered_output = (
                output.model_dump_json(indent=2)
                if hasattr(output, "model_dump_json")
                else json.dumps(output, ensure_ascii=False, indent=2)
                if isinstance(output, dict)
                else str(output)
            )
            ctx.ui.stream_final_block(f"{self.node_name} Final", rendered_output)
            ctx.ui.stream_end()
        if ctx.ui:
            ctx.ui.raise_if_interrupted()
        ctx.run_logger.log(
            "node_end",
            {
                "node": self.node_name,
                "output": output.model_dump() if hasattr(output, "model_dump") else str(output),
                "last_response_id": result.last_response_id,
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
        return output, result.last_response_id
