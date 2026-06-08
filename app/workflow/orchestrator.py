from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from app.config import Settings
from app.executors.mock_sql_executor import MockSqlExecutor
from app.nodes.agent import AgentNode
from app.schemas import FinalWorkflowOutput, PhasePlan, PhaseResult, PlannerOutput
from app.services.sql_fallback import build_fallback_phase_result
from app.services.run_context import SqlMateContext
from app.workflow.planner import PlannerPipeline


SQL_KEYWORDS = re.compile(
    r"^\s*(--|/\*|WITH\b|SELECT\b|INSERT\b|UPDATE\b|DELETE\b|CREATE\b|ALTER\b|DROP\b|"
    r"SET\b|RESET\b|VACUUM\b|ANALYZE\b|EXPLAIN\b|TRUNCATE\b|MERGE\b|GRANT\b|REVOKE\b|"
    r"BEGIN\b|COMMIT\b|ROLLBACK\b)",
    re.IGNORECASE,
)


def _looks_like_sql(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    hits = sum(1 for line in lines if SQL_KEYWORDS.match(line))
    return hits >= 2 or (hits == 1 and len(lines) <= 3)


def _is_recoverable_sql_path(path: str) -> bool:
    """只从明确的 SQL 备用字段中回收 SQL，避免把证据或资料摘录混进最终 SQL。"""
    if not path:
        return False
    path_lower = path.lower()
    if any(blocked in path_lower for blocked in ("knowledge_used", "evidence_checklist", "validation_notes", "unresolved_risks")):
        return False
    tail = path_lower.split(".")[-1]
    tail = tail.split("[", 1)[0]
    recoverable_tails = {
        "sql",
        "setup_sql",
        "ddl_sql",
        "core_sql",
        "before_sql",
        "op_sql",
        "after_sql",
        "sql_block",
        "sql_blocks",
    }
    return tail in recoverable_tails


def _collect_extra_sql(value: Any, path: str = "") -> list[tuple[str, str]]:
    extras: list[tuple[str, str]] = []
    if isinstance(value, str):
        if _is_recoverable_sql_path(path) and _looks_like_sql(value):
            extras.append((path or "value", value.strip()))
        return extras
    if isinstance(value, list):
        for idx, item in enumerate(value):
            extras.extend(_collect_extra_sql(item, f"{path}[{idx}]"))
        return extras
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "generated_sql":
                continue
            child_path = f"{path}.{key}" if path else str(key)
            extras.extend(_collect_extra_sql(item, child_path))
    return extras


def _phase_sql(stage: str, result: PhaseResult) -> tuple[str, list[str]]:
    data = result.model_dump()
    base_sql = (result.generated_sql or "").strip()
    extras = [
        (path, sql)
        for path, sql in _collect_extra_sql(data)
        if sql and sql not in base_sql
    ]
    notes: list[str] = []
    blocks = [base_sql] if base_sql else []
    for path, sql in extras:
        notes.append(path)
        blocks.append(f"-- recovered from {stage.lower()} PhaseResult.{path}\n{sql}")
    return "\n\n".join(blocks), notes


class WorkflowOrchestrator:
    """工作流总编排器。

    它负责把 planner、人工审批、三个并行阶段、SQL 合并和执行器验证
    串成一条完整主链路。
    """

    def __init__(
        self,
        planner: PlannerPipeline,
        setup_node: AgentNode,
        ddl_node: AgentNode,
        core_node: AgentNode,
        output_dir: Path,
        settings: Settings,
    ) -> None:
        self.planner = planner
        self.setup_node = setup_node
        self.ddl_node = ddl_node
        self.core_node = core_node
        self.output_dir = output_dir
        self.settings = settings

    async def run(self, ctx: SqlMateContext, user_input: dict) -> FinalWorkflowOutput:
        """执行完整业务流程并返回最终产物。"""
        planner_output, _planner_response_id = await self.planner.run(ctx, user_input)
        planner_output = await self._review_planner_output(ctx, user_input, planner_output)

        setup_plan = await self.planner.build_phase_plan(ctx, "SETUP", user_input, planner_output)
        planner_output.setup_plan = setup_plan
        ddl_plan = await self.planner.build_phase_plan(ctx, "DDL", user_input, planner_output)
        planner_output.ddl_plan = ddl_plan
        core_plan = await self.planner.build_phase_plan(ctx, "CORE", user_input, planner_output)
        planner_output.core_plan = core_plan

        setup_plan = await self._review_phase_plan(ctx, user_input, planner_output, "SETUP", setup_plan)
        planner_output.setup_plan = setup_plan
        ddl_plan = await self._review_phase_plan(ctx, user_input, planner_output, "DDL", ddl_plan)
        planner_output.ddl_plan = ddl_plan
        core_plan = await self._review_phase_plan(ctx, user_input, planner_output, "CORE", core_plan)
        planner_output.core_plan = core_plan

        async def run_phase(
            stage: str,
            node: AgentNode,
            plan: PhasePlan,
            upstream_artifacts: dict | None = None,
            ddl_generated_sql: str | None = None,
        ) -> PhaseResult:
            """执行单个阶段节点。

            这里会把 planner 输出、阶段计划和已有知识缓存统一打包给该阶段，
            同时明确告知节点：必须先完成 evidence 收集，再开始写 SQL。
            """
            stage_key = stage.lower()
            if ctx.ui:
                ctx.ui.enter_node(stage.upper(), stage_key, stage.upper(), f"executing {stage.lower()} phase")
            stage_payload = {
                "user_input": user_input,
                "global_plan": planner_output.global_plan.model_dump(),
                "phase_plan": plan.model_dump(),
                "cached_knowledge": planner_output.cached_knowledge.get(stage_key, {}).model_dump()
                if hasattr(planner_output.cached_knowledge.get(stage_key, {}), "model_dump")
                else {},
                "shared_contract": {
                    "required_setup_objects": planner_output.global_plan.required_setup_objects,
                    "validation_strategy": planner_output.global_plan.validation_strategy,
                    "sql_contract": planner_output.global_plan.sql_contract,
                },
                "knowledge_instruction": {
                    "must_collect_evidence_before_sql": True,
                    "continue_retrieving_until_uncertain_topics_are_resolved": True,
                },
            }
            if upstream_artifacts:
                stage_payload["upstream_artifacts"] = upstream_artifacts
            try:
                result, _ = await asyncio.wait_for(
                    node.run(ctx, stage_payload, previous_response_id=None),
                    timeout=self.settings.executor.phase_timeout_seconds,
                )
            except Exception as exc:
                if not self.settings.executor.allow_phase_fallback:
                    raise
                reason = str(exc) or repr(exc)
                ctx.run_logger.log(
                    "phase_fallback",
                    {"stage": stage, "reason": reason},
                )
                if ctx.ui:
                    ctx.ui.warning(f"{stage} phase fallback activated: {reason}")
                result = build_fallback_phase_result(stage, planner_output, plan)
            if ctx.ui:
                ctx.ui.leave_node(stage.upper(), stage_key, stage.upper(), "phase result ready")
            if not isinstance(result, PhaseResult):
                result = PhaseResult.model_validate(result)
            if ctx.ui:
                result_sql = getattr(result, "generated_sql", "") or ""
                if result_sql.strip():
                    ctx.ui.show_generated_sql(stage, result_sql)
            return result

        setup_result, ddl_result = await asyncio.gather(
            run_phase("SETUP", self.setup_node, setup_plan),
            run_phase("DDL", self.ddl_node, ddl_plan),
        )
        core_result = await run_phase(
            "CORE",
            self.core_node,
            core_plan,
            upstream_artifacts={
                "ddl": {
                    "generated_sql": ddl_result.generated_sql,
                    "evidence_checklist": ddl_result.evidence_checklist,
                    "validation_notes": ddl_result.validation_notes,
                    "unresolved_risks": ddl_result.unresolved_risks,
                }
            },
            ddl_generated_sql=ddl_result.generated_sql,
        )

        if ctx.ui:
            ctx.ui.mark_step("MERGE", "running", "assembling final sql bundle")
        setup_sql, setup_recovered = _phase_sql("SETUP", setup_result)
        ddl_sql, ddl_recovered = _phase_sql("DDL", ddl_result)
        core_sql, core_recovered = _phase_sql("CORE", core_result)
        merged_sql = "\n\n".join(
            [
                f"-- SETUP\n{setup_sql}",
                f"-- DDL\n{ddl_sql}",
                f"-- CORE\n{core_sql}",
            ]
        )
        recovered_sql_fields = {
            "setup": setup_recovered,
            "ddl": ddl_recovered,
            "core": core_recovered,
        }
        recovered_count = sum(len(items) for items in recovered_sql_fields.values())
        if recovered_count:
            ctx.run_logger.log(
                "sql_recovered_from_phase_result",
                {"fields": recovered_sql_fields, "count": recovered_count},
            )
            if ctx.ui:
                ctx.ui.warning(f"Recovered SQL-like content from {recovered_count} non-generated_sql fields.")
        if ctx.ui:
            ctx.ui.mark_step("MERGE", "done", "sql merged")

        executor = MockSqlExecutor()
        if ctx.ui:
            ctx.ui.mark_step("EXECUTE", "running", "validating merged sql")
        execution = executor.execute(merged_sql)
        if ctx.ui:
            ctx.ui.mark_step("EXECUTE", "done" if execution.success else "failed", execution.message)
            ctx.ui.update_summary(
                "Execution Result",
                [
                    f"Success: {execution.success}",
                    f"Executor: {execution.executor_type}",
                    f"Message: {execution.message}",
                    f"Failed Stage: {execution.failed_stage or '-'}",
                ],
            )
        final_output = FinalWorkflowOutput(
            planner_output=planner_output,
            setup_result=setup_result,
            ddl_result=ddl_result,
            core_result=core_result,
            merged_sql=merged_sql,
            execution=execution,
        )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.output_dir / f"{ctx.run_id}.json"
        sql_path = self.output_dir / f"{ctx.run_id}.sql"
        output_path.write_text(
            json.dumps(final_output.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        sql_path.write_text(merged_sql, encoding="utf-8")
        ctx.run_logger.log(
            "workflow_complete",
            {"output_path": str(output_path), "sql_path": str(sql_path), "success": execution.success},
        )
        if ctx.ui:
            ctx.ui.success(f"Workflow complete. Output saved to {output_path}")
            ctx.ui.note_output(f"SQL saved to: {sql_path}")
        return final_output

    async def _review_planner_output(
        self,
        ctx: SqlMateContext,
        user_input: dict,
        planner_output: PlannerOutput,
    ) -> PlannerOutput:
        """在 CLI 中展示 planner 输出，并在用户批准前允许反复修订。"""
        if not ctx.ui:
            return planner_output
        while True:
            decision = ctx.ui.review_planner_output(planner_output)
            ctx.run_logger.log("user_review", {"scope": "planner", "decision": decision.model_dump()})
            if decision.approved:
                return planner_output
            planner_output = await self.planner.revise_global_plan(
                ctx=ctx,
                user_input=user_input,
                planner_output=planner_output,
                feedback=decision.feedback,
            )

    async def _review_phase_plan(
        self,
        ctx: SqlMateContext,
        user_input: dict,
        planner_output: PlannerOutput,
        stage: str,
        plan: PhasePlan,
    ) -> PhasePlan:
        """在 CLI 中展示单阶段计划，并在用户批准前允许反复修订。"""
        if not ctx.ui:
            return plan
        current_plan = plan
        while True:
            decision = ctx.ui.review_phase_plan(stage, current_plan)
            ctx.run_logger.log(
                "user_review",
                {"scope": f"{stage.lower()}_plan", "decision": decision.model_dump()},
            )
            if decision.approved:
                return current_plan
            current_plan = await self.planner.refine_phase_plan(
                ctx=ctx,
                stage=stage,
                user_input=user_input,
                global_plan=planner_output,
                current_plan=current_plan,
                feedback=decision.feedback,
            )
