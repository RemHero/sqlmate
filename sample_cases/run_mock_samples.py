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
from app.schemas import GlobalPlan, KnowledgeBundle, PhasePlan, PhaseResult, PlannerOutput
from app.services.knowledge import KnowledgeService
from app.services.prompt_loader import PromptLoader
from app.services.run_context import SqlMateContext
from app.ui.console import WorkflowConsole
from app.workflow.orchestrator import WorkflowOrchestrator


class SamplePlanner:
    """样例生成脚本使用的假 planner。"""

    def __init__(self, sample_name: str) -> None:
        self.sample_name = sample_name

    async def run(self, ctx: SqlMateContext, user_input: dict):
        """针对单个样例输入生成稳定的 mock planner 输出。"""
        planner_output = PlannerOutput(
            global_plan=GlobalPlan(
                task_goal=user_input["task_goal"],
                db_dialect=user_input["db_dialect"],
                feature_under_test=user_input["feature_under_test"],
                must_cover=["positive path", "negative path", "cleanup"],
                required_setup_objects=[
                    {"type": "database", "name": f"{self.sample_name}_db"},
                    {
                        "type": "table",
                        "name": f"{self.sample_name}_t1",
                        "columns": "id INT, name VARCHAR(32)",
                        "primary_key": "id",
                    },
                ],
                ddl_topics_needed=["create table"],
                core_topics_needed=["insert", "query validation"],
                validation_strategy=["assert row count", "assert error path"],
                sql_contract={
                    "must_be_executable": True,
                    "must_include_cleanup": True,
                    "must_avoid_pseudo_sql": True,
                },
                planning_considerations=["mock sample run"],
                risks=["prompt content still placeholder"],
            ),
            setup_plan=PhasePlan(
                stage="SETUP",
                objective="prepare objects",
                required_topics=["session setup"],
                evidence_requirements=["confirm setup syntax"],
            ),
            ddl_plan=PhasePlan(
                stage="DDL",
                objective="create required tables",
                required_topics=["create table"],
                evidence_requirements=["confirm DDL syntax"],
            ),
            core_plan=PhasePlan(
                stage="CORE",
                objective="insert and validate",
                required_topics=["insert", "query validation"],
                evidence_requirements=["confirm DML and query syntax"],
            ),
            cached_knowledge={
                "setup": KnowledgeBundle(stage="setup", distilled_summary="mock setup evidence"),
                "ddl": KnowledgeBundle(stage="ddl", distilled_summary="mock ddl evidence"),
                "core": KnowledgeBundle(stage="core", distilled_summary="mock core evidence"),
            },
            debate_summary=["mock sample debate"],
        )
        return planner_output, "mock_response"

    async def revise_global_plan(self, ctx, user_input, planner_output, feedback):
        """模拟总计划在接收反馈后的修订。"""
        planner_output.debate_summary.append(f"feedback:{feedback}")
        return planner_output

    async def refine_phase_plan(self, ctx, stage, user_input, global_plan, current_plan, feedback):
        """模拟子计划在接收反馈后的修订。"""
        current_plan.test_focus.append(f"feedback:{feedback}")
        return current_plan


class SamplePhaseNode:
    """样例生成脚本使用的假阶段节点。"""

    def __init__(self, stage: str, sample_name: str) -> None:
        self.stage = stage
        self.sample_name = sample_name

    async def run(self, ctx: SqlMateContext, payload: dict, previous_response_id: str | None = None):
        """根据样例名拼出一套稳定 SQL，用于验证样例产物链路。"""
        db_name = f"{self.sample_name}_db"
        table_name = f"{self.sample_name}_t1"
        sql_map = {
            "SETUP": f"CREATE DATABASE IF NOT EXISTS {db_name};",
            "DDL": f"CREATE TABLE {db_name}.{table_name}(id INT, name VARCHAR(32));",
            "CORE": "\n".join(
                [
                    f"INSERT INTO {db_name}.{table_name} VALUES (1, 'alpha');",
                    f"SELECT COUNT(*) FROM {db_name}.{table_name};",
                    f"DROP TABLE IF EXISTS {db_name}.{table_name};",
                    f"DROP DATABASE IF EXISTS {db_name};",
                ]
            ),
        }
        result = PhaseResult(
            stage=self.stage,
            plan_summary=f"{self.stage} mock summary",
            knowledge_used=KnowledgeBundle(stage=self.stage.lower(), distilled_summary=f"{self.stage} mock evidence"),
            generated_sql=sql_map[self.stage],
            validation_notes=["mock run"],
            unresolved_risks=[],
            evidence_checklist=[f"{self.stage} evidence confirmed in mock mode"],
        )
        return result, None


async def run_single_case(case_path: Path) -> dict:
    """执行单个样例输入，并把输出与 transcript 落盘。"""
    sample_name = case_path.stem
    run_id = f"sample-{sample_name}-{uuid.uuid4().hex[:8]}"
    ctx = SqlMateContext(
        run_id=run_id,
        run_logger=JsonlRunLogger(ROOT / "logs" / f"{run_id}.jsonl"),
        prompt_loader=PromptLoader(ROOT / "node_prompts"),
        knowledge_service=KnowledgeService(ROOT / "knowledge"),
        output_dir=ROOT / "sample_cases" / "outputs",
        ui=WorkflowConsole(approval_mode="auto", show_banner=False, show_raw_json=False),
    )
    orchestrator = WorkflowOrchestrator(
        planner=SamplePlanner(sample_name),
        setup_node=SamplePhaseNode("SETUP", sample_name),
        ddl_node=SamplePhaseNode("DDL", sample_name),
        core_node=SamplePhaseNode("CORE", sample_name),
        output_dir=ROOT / "sample_cases" / "outputs",
        settings=SimpleNamespace(
            executor=SimpleNamespace(
                phase_timeout_seconds=120,
                allow_phase_fallback=True,
            )
        ),
    )
    user_input = json.loads(case_path.read_text(encoding="utf-8"))
    result = await orchestrator.run(ctx, user_input)
    transcript = {
        "sample": sample_name,
        "input_file": str(case_path.relative_to(ROOT)),
        "output_file": f"sample_cases/outputs/{run_id}.json",
        "execution_success": result.execution.success,
    }
    transcript_path = ROOT / "sample_cases" / "transcripts" / f"{run_id}.json"
    transcript_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    return transcript


async def main() -> int:
    """批量执行 `sample_cases/inputs` 下的所有 mock 样例。"""
    inputs_dir = ROOT / "sample_cases" / "inputs"
    cases = sorted(inputs_dir.glob("*.json"))
    results = []
    for case_path in cases:
        results.append(await run_single_case(case_path))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
