from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.logging_setup import JsonlRunLogger
from app.schemas import GlobalPlan, KnowledgeBundle, PhaseOutline, PhasePlan, PhaseResult, PlannerOutput
from app.services.knowledge import KnowledgeService
from app.services.prompt_loader import PromptLoader
from app.services.run_context import SqlMateContext
from app.ui.console import WorkflowConsole
from app.workflow.orchestrator import WorkflowOrchestrator


class FakePlanner:
    """用于 smoke test 的假 planner。

    它绕过真实模型调用，直接返回一份稳定的结构化计划，
    以便验证编排和落盘逻辑是否正常。
    """

    async def run(self, ctx: SqlMateContext, user_input: dict):
        planner_output = PlannerOutput(
            global_plan=GlobalPlan(
                task_goal=user_input["task_goal"],
                db_dialect=user_input["db_dialect"],
                feature_under_test=user_input["feature_under_test"],
                must_cover=["setup", "ddl", "core"],
                required_setup_objects=[
                    {
                        "type": "table",
                        "name": "t_smoke",
                        "description": "smoke workflow table with id and name columns",
                    },
                ],
                validation_strategy=["check rows"],
                sql_contract={
                    "must_be_executable": True,
                    "must_include_cleanup": True,
                    "must_avoid_pseudo_sql": True,
                },
                planning_considerations=["smoke test"],
                risks=[],
            ),
            setup_outline=PhaseOutline(
                stage="SETUP",
                objective="prepare environment",
                coverage_focus=["database lifecycle"],
                test_dimensions=["idempotent initialization"],
                stage_boundaries=["no table DDL"],
                shared_dependencies=["use smoke_db"],
                success_criteria=["DDL can execute in smoke_db"],
                deferred_planning_questions=["whether user setup is required"],
            ),
            ddl_outline=PhaseOutline(
                stage="DDL",
                objective="create table",
                coverage_focus=["table creation"],
                test_dimensions=["positive DDL", "constraint modeling"],
                stage_boundaries=["no DML assertions"],
                shared_dependencies=["use t_smoke contract"],
                success_criteria=["CORE receives stable table"],
                deferred_planning_questions=["exact DDL syntax by dialect version"],
            ),
            core_outline=PhaseOutline(
                stage="CORE",
                objective="insert and query",
                coverage_focus=["insert", "query validation"],
                test_dimensions=["positive DML", "cleanup"],
                stage_boundaries=["no table redesign"],
                shared_dependencies=["table already exists"],
                success_criteria=["data path can be validated"],
                deferred_planning_questions=["exact error assertions if any"],
            ),
            cached_knowledge={
                "setup": KnowledgeBundle(stage="setup", distilled_summary="setup knowledge"),
                "ddl": KnowledgeBundle(stage="ddl", distilled_summary="ddl knowledge"),
                "core": KnowledgeBundle(stage="core", distilled_summary="core knowledge"),
            },
            debate_summary=["smoke"],
        )
        return planner_output, "resp_smoke"

    async def build_phase_plan(self, ctx, stage, user_input, planner_output):
        """模拟阶段节点基于 outline 生成详细计划。"""
        if stage == "SETUP":
            return PhasePlan(
                stage="SETUP",
                objective="prepare",
                test_focus=["create database"],
                input_contract=["admin privilege available"],
                output_contract=["smoke_db is ready"],
                required_topics=["session setup"],
                assumptions=["database can be recreated"],
                knowledge_gaps=[],
                evidence_requirements=["verify object lifecycle syntax"],
            )
        if stage == "DDL":
            return PhasePlan(
                stage="DDL",
                objective="create table",
                test_focus=["create t_smoke"],
                input_contract=["smoke_db exists"],
                output_contract=["t_smoke exists"],
                required_topics=["create table"],
                assumptions=["id is primary key"],
                knowledge_gaps=[],
                evidence_requirements=["verify create table syntax"],
            )
        return PhasePlan(
            stage="CORE",
            objective="insert and query",
            test_focus=["insert row", "query row", "cleanup"],
            input_contract=["t_smoke exists"],
            output_contract=["core SQL generated"],
            required_topics=["insert"],
            assumptions=["single table flow"],
            knowledge_gaps=[],
            evidence_requirements=["verify insert and query syntax"],
        )

    async def revise_global_plan(self, ctx, user_input, planner_output, feedback):
        """模拟用户反馈后对总计划的修订。"""
        planner_output.debate_summary.append(f"user_feedback:{feedback}")
        return planner_output

    async def refine_phase_plan(self, ctx, stage, user_input, global_plan, current_plan, feedback):
        """模拟用户反馈后对单阶段计划的修订。"""
        current_plan.test_focus.append(f"user_feedback:{feedback}")
        return current_plan


class FakePhaseNode:
    """用于 smoke test 的假阶段节点。"""

    def __init__(self, stage: str, sql: str) -> None:
        self.stage = stage
        self.sql = sql

    async def run(self, ctx: SqlMateContext, payload: dict, previous_response_id: str | None = None):
        """返回预定义的阶段结果，避免依赖真实模型。"""
        result = PhaseResult(
            stage=self.stage,
            plan_summary=f"{self.stage} summary",
            knowledge_used=KnowledgeBundle(stage=self.stage.lower(), distilled_summary="used"),
            generated_sql=self.sql,
            validation_notes=["smoke"],
            unresolved_risks=[],
            evidence_checklist=["smoke evidence"],
        )
        return result, None


async def main() -> int:
    """执行一次离线 smoke test。"""
    root = Path.cwd()
    run_id = f"smoke-{uuid.uuid4().hex[:8]}"
    ctx = SqlMateContext(
        run_id=run_id,
        run_logger=JsonlRunLogger(root / "logs" / f"{run_id}.jsonl"),
        prompt_loader=PromptLoader(root / "node_prompts"),
        knowledge_service=KnowledgeService(root / "knowledge"),
        output_dir=root / "output",
        ui=WorkflowConsole(approval_mode="auto", show_banner=False, show_raw_json=False),
    )
    orchestrator = WorkflowOrchestrator(
        planner=FakePlanner(),
        setup_node=FakePhaseNode("SETUP", "CREATE DATABASE IF NOT EXISTS smoke_db;"),
        ddl_node=FakePhaseNode("DDL", "CREATE TABLE smoke_db.t_smoke(id INT, name VARCHAR(10));"),
        core_node=FakePhaseNode(
            "CORE",
            "INSERT INTO smoke_db.t_smoke VALUES (1, 'a');\nSELECT * FROM smoke_db.t_smoke;\nDROP TABLE IF EXISTS smoke_db.t_smoke;",
        ),
        output_dir=root / "output",
        settings=SimpleNamespace(
            executor=SimpleNamespace(
                phase_timeout_seconds=120,
                allow_phase_fallback=True,
            )
        ),
    )
    user_input = json.loads((root / "tests" / "demo_input.json").read_text(encoding="utf-8"))
    result = await orchestrator.run(ctx, user_input)
    print(json.dumps({"success": result.execution.success, "output_file": f"output/{run_id}.json"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
