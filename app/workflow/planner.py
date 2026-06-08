from __future__ import annotations

from app.nodes.agent import AgentNode
from app.nodes.llm import LLMNode
from app.schemas import KnowledgeBundle, PhasePlan, PlannerDraft, PlannerOutput, PlannerReview
from app.services.run_context import SqlMateContext


def _draft_to_output(draft: PlannerDraft) -> PlannerOutput:
    """把 draft 结果本地转换成正式 planner 输出。

    这样做的目标是避免把一大段 draft 结构再次整段发送给 critic/finalizer，
    从而显著降低延迟和失败概率。当前默认把 draft 直接提升为正式计划，
    仅在需要时再启用更重的对抗/修订链路。
    """
    return PlannerOutput(
        global_plan=draft.global_plan,
        setup_outline=draft.setup_outline,
        ddl_outline=draft.ddl_outline,
        core_outline=draft.core_outline,
        cached_knowledge={
            "setup": KnowledgeBundle(stage="setup"),
            "ddl": KnowledgeBundle(stage="ddl"),
            "core": KnowledgeBundle(stage="core"),
        },
        debate_summary=["Planner fast path enabled: draft promoted to global planning spec."],
    )


class PlannerPipeline:
    """负责 planner 主流程的编排器。

    它把高层规划拆成四个阶段：
    1. Draft：生成初版总计划
    2. Critic：进行质疑和挑战
    3. Finalizer：整合评审意见并产出正式计划
    4. Revision / Refiner：接收用户反馈后修订总计划或子计划
    """

    def __init__(
        self,
        draft_node: AgentNode | LLMNode,
        critic_node: LLMNode,
        finalizer_node: AgentNode,
        revision_node: AgentNode,
        phase_refiners: dict[str, AgentNode],
        debate_rounds: int,
    ) -> None:
        self.draft_node = draft_node
        self.critic_node = critic_node
        self.finalizer_node = finalizer_node
        self.revision_node = revision_node
        self.phase_refiners = phase_refiners
        self.debate_rounds = debate_rounds

    @staticmethod
    def _sync_stage_cache(ctx: SqlMateContext, planner_output: PlannerOutput, stage: str) -> None:
        stage_key = stage.lower()
        raw_bundle = ctx.shared_state.setdefault("knowledge_cache", {}).get(stage_key)
        if raw_bundle:
            planner_output.cached_knowledge[stage_key] = KnowledgeBundle.model_validate(raw_bundle)
        else:
            planner_output.cached_knowledge.setdefault(stage_key, KnowledgeBundle(stage=stage_key))

    @staticmethod
    def _sync_all_known_caches(ctx: SqlMateContext, planner_output: PlannerOutput) -> None:
        cache = ctx.shared_state.setdefault("knowledge_cache", {})
        for stage_key in ("planner", "shared", "setup", "ddl", "core"):
            raw_bundle = cache.get(stage_key)
            if raw_bundle:
                planner_output.cached_knowledge[stage_key] = KnowledgeBundle.model_validate(raw_bundle)
            else:
                planner_output.cached_knowledge.setdefault(stage_key, KnowledgeBundle(stage=stage_key))

    async def run(self, ctx: SqlMateContext, user_input: dict) -> tuple[PlannerOutput, str | None]:
        """执行 planner 主链路并返回最终结构化计划。

        当前 planner 只负责高层测试规格拆解，不提前决定各阶段的语法实现细节。
        """
        if ctx.ui:
            ctx.ui.enter_node(
                "PLANNER_DRAFT",
                "planner",
                "PLANNER_DRAFT",
                "building planner draft",
            )
        draft, draft_response_id = await self.draft_node.run(ctx, {"user_input": user_input})
        if not isinstance(draft, PlannerDraft):
            draft = PlannerDraft.model_validate(draft)
        if ctx.ui:
            ctx.ui.leave_node("PLANNER_DRAFT", "planner", "PLANNER_DRAFT", "planner draft ready")

        final_output = _draft_to_output(draft)
        self._sync_all_known_caches(ctx, final_output)
        if ctx.ui:
            ctx.ui.mark_step("PLANNER_CRITIC", "done", "skipped in fast path")
            ctx.ui.mark_step("PLANNER_FINALIZER", "done", "local finalize")
            ctx.ui.update_summary(
                "Planner Draft",
                [
                    "Planner draft generated from agent node with knowledge-index capability.",
                    f"Setup focus: {', '.join(final_output.setup_outline.coverage_focus) or '-'}",
                    f"DDL focus: {', '.join(final_output.ddl_outline.coverage_focus) or '-'}",
                    f"Core focus: {', '.join(final_output.core_outline.coverage_focus) or '-'}",
                ],
            )
        return final_output, draft_response_id

    async def revise_global_plan(
        self,
        ctx: SqlMateContext,
        user_input: dict,
        planner_output: PlannerOutput,
        feedback: str,
    ) -> PlannerOutput:
        """根据用户反馈修订总计划。"""
        if ctx.ui:
            ctx.ui.enter_node("PLANNER_REVIEW", "planner", "PLANNER_REVISION", "revising planner with feedback")
        payload = {
            "user_input": user_input,
            "current_plan": planner_output.model_dump(),
            "user_feedback": feedback,
        }
        revised_output, _ = await self.revision_node.run(ctx, payload)
        if ctx.ui:
            ctx.ui.leave_node("PLANNER_REVIEW", "planner", "PLANNER_REVISION", "planner revision ready")
        if not isinstance(revised_output, PlannerOutput):
            revised_output = PlannerOutput.model_validate(revised_output)
        return revised_output

    async def build_phase_plan(
        self,
        ctx: SqlMateContext,
        stage: str,
        user_input: dict,
        planner_output: PlannerOutput,
    ) -> PhasePlan:
        """让阶段节点基于 global plan 和 phase outline 自主生成详细计划。"""
        planner = self.phase_refiners[stage.upper()]
        stage_key = stage.lower()
        outline = getattr(planner_output, f"{stage_key}_outline")
        if ctx.ui:
            ctx.ui.enter_node(f"{stage.upper()}_PLAN", stage_key, f"{stage.upper()}_PLAN_REFINER", "building detailed phase plan")
        payload = {
            "user_input": user_input,
            "global_plan": planner_output.global_plan.model_dump(),
            "phase_outline": outline.model_dump(),
            "existing_phase_plans": {
                key: plan.model_dump()
                for key, plan in {
                    "setup": planner_output.setup_plan,
                    "ddl": planner_output.ddl_plan,
                    "core": planner_output.core_plan,
                }.items()
                if plan is not None
            },
            "cached_knowledge": planner_output.cached_knowledge.get(stage_key, {}).model_dump()
            if hasattr(planner_output.cached_knowledge.get(stage_key, {}), "model_dump")
            else {},
            "shared_cached_knowledge": planner_output.cached_knowledge.get("shared", {}).model_dump()
            if hasattr(planner_output.cached_knowledge.get("shared", {}), "model_dump")
            else {},
            "shared_contract": {
                "required_setup_objects": planner_output.global_plan.required_setup_objects,
                "validation_strategy": planner_output.global_plan.validation_strategy,
                "sql_contract": planner_output.global_plan.sql_contract,
            },
            "knowledge_instruction": {
                "must_collect_evidence_before_plan": True,
                "focus_on_syntax_and_execution_details": True,
            },
        }
        generated_plan, _ = await planner.run(ctx, payload)
        if ctx.ui:
            ctx.ui.leave_node(f"{stage.upper()}_PLAN", stage_key, f"{stage.upper()}_PLAN_REFINER", "detailed phase plan ready")
        if not isinstance(generated_plan, PhasePlan):
            generated_plan = PhasePlan.model_validate(generated_plan)
        self._sync_stage_cache(ctx, planner_output, stage)
        self._sync_stage_cache(ctx, planner_output, "shared")
        return generated_plan

    async def refine_phase_plan(
        self,
        ctx: SqlMateContext,
        stage: str,
        user_input: dict,
        global_plan: PlannerOutput,
        current_plan: PhasePlan,
        feedback: str,
    ) -> PhasePlan:
        """根据用户反馈修订单个阶段的子计划。"""
        refiner = self.phase_refiners[stage.upper()]
        review_step = f"{stage.upper()}_PLAN_REVIEW"
        if ctx.ui:
            ctx.ui.enter_node(review_step, stage.lower(), f"{stage.upper()}_PLAN_REFINER", "revising subplan with feedback")
        payload = {
            "user_input": user_input,
            "global_plan": global_plan.global_plan.model_dump(),
            "phase_outline": getattr(global_plan, f"{stage.lower()}_outline").model_dump(),
            "current_phase_plan": current_plan.model_dump(),
            "existing_phase_plans": {
                key: plan.model_dump()
                for key, plan in {
                    "setup": global_plan.setup_plan,
                    "ddl": global_plan.ddl_plan,
                    "core": global_plan.core_plan,
                }.items()
                if plan is not None and key != stage.lower()
            },
            "cached_knowledge": global_plan.cached_knowledge.get(stage.lower(), {}).model_dump()
            if hasattr(global_plan.cached_knowledge.get(stage.lower(), {}), "model_dump")
            else {},
            "shared_cached_knowledge": global_plan.cached_knowledge.get("shared", {}).model_dump()
            if hasattr(global_plan.cached_knowledge.get("shared", {}), "model_dump")
            else {},
            "shared_contract": {
                "required_setup_objects": global_plan.global_plan.required_setup_objects,
                "validation_strategy": global_plan.global_plan.validation_strategy,
                "sql_contract": global_plan.global_plan.sql_contract,
            },
            "knowledge_instruction": {
                "must_collect_evidence_before_plan": True,
                "focus_on_syntax_and_execution_details": True,
            },
            "user_feedback": feedback,
        }
        revised_plan, _ = await refiner.run(ctx, payload)
        if ctx.ui:
            ctx.ui.leave_node(review_step, stage.lower(), f"{stage.upper()}_PLAN_REFINER", "subplan revision ready")
        if not isinstance(revised_plan, PhasePlan):
            revised_plan = PhasePlan.model_validate(revised_plan)
        self._sync_stage_cache(ctx, global_plan, stage)
        self._sync_stage_cache(ctx, global_plan, "shared")
        return revised_plan
