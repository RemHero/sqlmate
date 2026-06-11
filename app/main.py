from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.config import load_settings
from app.logging_setup import JsonlRunLogger, configure_logging
from app.services.checkpoint import CheckpointManager
from app.nodes.agent import AgentNode
from app.nodes.llm import LLMNode
from app.schemas import GlobalPlan, KnowledgeBundle, PhaseOutline, PhasePlan, PhaseResult, PlannerDraft, PlannerOutput, PlannerReview
from app.services.knowledge import KnowledgeService
from app.services.prompt_loader import PromptLoader
from app.services.providers import ProviderRegistry
from app.services.run_context import SqlMateContext
from app.tools import build_knowledge_tools
from app.tracing import build_run_config
from app.ui.console import WorkflowConsole, WorkflowInterrupted
from app.workflow.orchestrator import WorkflowOrchestrator
from app.workflow.planner import PlannerPipeline


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    这里同时支持：
    1. 指定配置文件与输入文件
    2. 覆盖审批模式
    3. 关闭启动 Banner
    """
    parser = argparse.ArgumentParser(description="Run the SqlMate workflow.")
    parser.add_argument(
        "--config",
        default="config/settings.example.yaml",
        help="Path to settings yaml.",
    )
    parser.add_argument("--input-file", required=False, help="Path to input json.")
    parser.add_argument(
        "--approval-mode",
        choices=["interactive", "auto"],
        default=None,
        help="Override the UI approval mode.",
    )
    parser.add_argument(
        "--ui-preview",
        action="store_true",
        help="Run a mocked planner/subplanner preview to inspect the CLI without real API calls.",
    )
    parser.add_argument(
        "--show-think",
        action="store_true",
        help="Print intermediate reasoning to the terminal. By default it is only written to logs.",
    )
    parser.add_argument(
        "--resume",
        metavar="RUN_ID",
        default=None,
        help="Resume from the most advanced checkpoint of a previous run.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force delete existing checkpoints and restart the run from scratch.",
    )
    return parser.parse_args()


def _timestamp_run_id() -> str:
    """生成基于当前时间的运行 ID，便于定位日志和输出。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def _load_user_input(path: str | Path) -> dict[str, Any]:
    """从 JSON 或纯文本文件加载用户请求。"""
    raw = Path(path).read_text(encoding="utf-8")
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return json.loads(raw)
    request = raw.strip()
    return {
        "task_goal": request,
        "db_dialect": "unknown",
        "feature_under_test": request,
        "raw_request": request,
        "requirements": [],
        "constraints": [],
    }


def _build_mock_planner_output(user_input: dict[str, Any]) -> PlannerOutput:
    """构建一份专门用于 UI 预览的假 planner 输出。

    这份数据不追求真实业务正确性，目标只是稳定展示：
    1. 总计划如何在页面上打印
    2. 子计划如何逐个审批
    3. 审批动作如何自然留在 shell 历史中
    """
    goal = user_input.get("task_goal", "请生成 SQL 回归用例")
    feature = user_input.get("feature_under_test", goal)
    global_plan = GlobalPlan(
        task_goal=goal,
        db_dialect=user_input.get("db_dialect", "unknown") or "unknown",
        feature_under_test=feature,
        must_cover=[
            "正向建表路径",
            "基础插入与查询验证",
            "重复键异常路径",
            "完整 cleanup",
        ],
        required_setup_objects=[
            {"type": "table", "name": "t_preview_feature", "description": "用于 UI preview 的最小回归测试表"},
        ],
        validation_strategy=[
            "检查结果行数",
            "检查精确返回值",
            "检查异常路径是否触发",
        ],
        sql_contract={
            "must_be_executable": True,
            "must_include_cleanup": True,
            "must_avoid_pseudo_sql": True,
        },
        planning_considerations=[
            "先固定表结构，避免并行阶段出现对象定义漂移",
            "setup、ddl、core 的对象命名必须共享同一个契约",
        ],
        risks=[
            "这是 UI preview mock 数据，不代表真实模型输出。",
        ],
    )
    setup_outline = PhaseOutline(
        stage="SETUP",
        objective="准备独立测试环境并固定执行前置条件",
        coverage_focus=["测试库生命周期", "会话初始化", "环境隔离"],
        test_dimensions=["对象清理顺序", "幂等初始化", "权限前置假设"],
        stage_boundaries=["不负责建表定义", "不负责核心业务断言"],
        shared_dependencies=["使用 preview_regression_db 作为统一测试库"],
        success_criteria=["后续 DDL 可直接在 preview_regression_db 中执行"],
        deferred_planning_questions=["是否需要创建专用用户或角色"],
    )
    ddl_outline = PhaseOutline(
        stage="DDL",
        objective="围绕统一对象契约规划结构类测试覆盖",
        coverage_focus=["主表创建", "对象存在性校验", "约束建模"],
        test_dimensions=["正向建表", "幂等建表", "非法 DDL 异常"],
        stage_boundaries=["不负责实际插入数据", "不负责业务查询断言"],
        shared_dependencies=["必须围绕 t_preview_feature 表定义展开"],
        success_criteria=["后续 CORE 可依赖稳定表结构执行"],
        deferred_planning_questions=["具体字段类型和分桶语法需要阶段节点取证"],
    )
    core_outline = PhaseOutline(
        stage="CORE",
        objective="围绕既定表结构规划数据操作与结果验证",
        coverage_focus=["插入路径", "查询断言", "异常路径", "cleanup"],
        test_dimensions=["正向 DML", "边界值", "重复键异常", "结果一致性"],
        stage_boundaries=["不重新设计表结构", "不引入与 DDL 冲突的约束假设"],
        shared_dependencies=["依赖 t_preview_feature 已成功创建"],
        success_criteria=["能够验证数据写入、读取和异常反馈"],
        deferred_planning_questions=["具体断言 SQL 与错误码需阶段节点结合语法证据细化"],
    )
    setup_plan = PhasePlan(
        stage="SETUP",
        objective="创建独立测试数据库，并准备最小环境",
        test_focus=["删除旧数据库", "创建新数据库", "切换当前数据库"],
        input_contract=["拥有创建和删除数据库权限"],
        output_contract=["后续 DDL 阶段可直接在 preview_regression_db 下执行"],
        required_topics=["session setup", "cleanup order"],
        optional_topics=[],
        assumptions=["环境允许重复回归执行"],
        knowledge_gaps=[],
        evidence_requirements=["确认当前会话或 schema 初始化语法"],
    )
    ddl_plan = PhasePlan(
        stage="DDL",
        objective="创建主测试表并固定表结构契约",
        test_focus=["创建主键表", "显式指定分桶", "验证表结构可 describe"],
        input_contract=["测试数据库已创建"],
        output_contract=["表 t_preview_feature 成功创建"],
        required_topics=["create table", "primary key", "distributed by hash"],
        optional_topics=[],
        assumptions=["使用 id 作为主键"],
        knowledge_gaps=[],
        evidence_requirements=["确认目标数据库主键表建表语法"],
    )
    core_plan = PhasePlan(
        stage="CORE",
        objective="完成插入、查询断言、异常验证和清理",
        test_focus=["插入 3 行样本数据", "验证 count 和精确值", "制造重复键冲突", "执行清理"],
        input_contract=["主测试表已创建完成"],
        output_contract=["得到可执行 core SQL 片段"],
        required_topics=["insert", "select validation", "duplicate key error"],
        optional_topics=[],
        assumptions=["异常路径以重复主键插入触发"],
        knowledge_gaps=[],
        evidence_requirements=["确认 insert/select/error path 的语法证据"],
    )
    return PlannerOutput(
        global_plan=global_plan,
        setup_outline=setup_outline,
        ddl_outline=ddl_outline,
        core_outline=core_outline,
        setup_plan=setup_plan,
        ddl_plan=ddl_plan,
        core_plan=core_plan,
        cached_knowledge={
            "setup": KnowledgeBundle(stage="setup"),
            "ddl": KnowledgeBundle(stage="ddl"),
            "core": KnowledgeBundle(stage="core"),
        },
        debate_summary=["UI preview mock output."],
    )


def _run_ui_preview(ui: WorkflowConsole, user_input: dict[str, Any]) -> None:
    """执行一轮纯 mock 的 UI 预览。"""
    planner_output = _build_mock_planner_output(user_input)
    while True:
        decision = ui.review_planner_output(planner_output)
        if decision.approved:
            break
        ui.warning("Mock preview 暂不根据反馈重写总计划，直接保留当前版本继续展示。")
    for stage, plan in [
        ("SETUP", planner_output.setup_plan),
        ("DDL", planner_output.ddl_plan),
        ("CORE", planner_output.core_plan),
    ]:
        while True:
            decision = ui.review_phase_plan(stage, plan)
            if decision.approved:
                break
            ui.warning(f"Mock preview 暂不根据反馈重写 {stage} 计划，直接保留当前版本继续展示。")
    ui.note_output("Mock preview finished. No API calls were made.")


def _build_node(
    name: str,
    settings,
    registry: ProviderRegistry,
    run_config,
    output_type,
    kind: str,
    tools: list[Any] | None = None,
    force_first_tool: bool = False,
):
    """根据节点名和配置构建具体节点实例。

    当前系统中存在两类节点：
    1. `LLMNode`：适合单轮结构化生成
    2. `AgentNode`：适合多轮工具调用、动态知识检索和修订场景
    """
    binding = settings.node_bindings[name]
    provider_cfg = settings.providers[binding.provider]
    model = registry.get_model(binding.provider)
    default_turns = settings.planner.max_turns if name.startswith("PLANNER") else 6
    max_turns = binding.max_turns or default_turns
    if kind == "llm":
        return LLMNode(
            name,
            model=model,
            output_type=output_type,
            run_config=run_config,
            max_turns=max_turns,
            model_extra_body=provider_cfg.model_extra_body,
        )
    return AgentNode(
        name,
        model=model,
        output_type=output_type,
        run_config=run_config,
        max_turns=max_turns,
        tools=tools,
        force_first_tool=force_first_tool,
        model_extra_body=provider_cfg.model_extra_body,
        max_output_tokens=settings.planner.max_output_tokens,
        max_tool_rounds=settings.planner.max_tool_rounds,
        supports_json_schema=provider_cfg.supports_json_schema,
    )


async def async_main() -> int:
    """组装整个应用并执行一次完整工作流。

    这是运行时真正的入口，负责：
    1. 加载环境变量与配置
    2. 初始化日志、知识服务、UI、Provider Registry
    3. 构建 planner、phase nodes 和 orchestrator
    4. 执行工作流并输出简洁完成摘要
    """
    load_dotenv()
    args = parse_args()
    settings = load_settings(args.config)

    root = Path.cwd()
    app_paths = settings.app
    log_dir = root / app_paths.log_dir
    output_dir = root / app_paths.output_dir
    prompts_dir = root / app_paths.prompts_dir
    knowledge_dir = root / app_paths.knowledge_dir
    doc_knowledge_dir = Path(settings.knowledge.doc_knowledge_dir)
    if not doc_knowledge_dir.is_absolute():
        doc_knowledge_dir = root / doc_knowledge_dir
    doc_knowledge_dirs: list[Path] = []
    configured_doc_dirs = settings.knowledge.doc_knowledge_dirs or [settings.knowledge.doc_knowledge_dir]
    for configured_dir in configured_doc_dirs:
        doc_dir = Path(configured_dir)
        if not doc_dir.is_absolute():
            doc_dir = root / doc_dir
        if doc_dir not in doc_knowledge_dirs:
            doc_knowledge_dirs.append(doc_dir)
    sql_examples_dir = Path(settings.knowledge.sql_examples_dir)
    if not sql_examples_dir.is_absolute():
        sql_examples_dir = root / sql_examples_dir
    sql_examples_dir.mkdir(parents=True, exist_ok=True)

    ui = WorkflowConsole(
        approval_mode=args.approval_mode or settings.ui.approval_mode,
        show_banner=settings.ui.show_banner,
        show_raw_json=settings.ui.show_raw_json,
        show_think_stream=args.show_think or settings.ui.show_think_stream,
    )
    ui.render_banner()

    # ---- 安装自定义 SIGINT 处理器，替代 asyncio.run() 的内置处理器 ----
    loop = asyncio.get_running_loop()
    intr_count = 0

    def _handle_sigint(signum, frame):
        """自定义 SIGINT 处理器，实现双击 Ctrl-C 优雅退出逻辑。

        第一次 Ctrl-C：取消所有正在运行的异步任务，中断当前操作。
        第二次 Ctrl-C：立即退出程序（返回码 130）。
        """
        nonlocal intr_count
        intr_count += 1
        if intr_count >= 2:
            # 双击 Ctrl-C：直接退出，恢复默认行为
            os.write(2, b"\nSqlMate: Detected second Ctrl+C. Exiting.\n")
            os._exit(130)
        # 第一次 Ctrl-C：取消所有运行中的任务
        for task in asyncio.all_tasks(loop):
            if not task.done():
                task.cancel()

    _old_sigint = signal.signal(signal.SIGINT, _handle_sigint)
    # --------------------------------------------------------------------

    prompt_loader = PromptLoader(prompts_dir)
    knowledge_service = KnowledgeService(
        knowledge_dir,
        max_matches_per_topic=settings.knowledge.max_matches_per_topic,
        enable_claude_index=settings.knowledge.enable_claude_index,
        claude_command=settings.knowledge.claude_command,
        claude_permission_mode=settings.knowledge.claude_permission_mode,
        claude_use_add_dir=settings.knowledge.claude_use_add_dir,
        claude_timeout_seconds=settings.knowledge.claude_timeout_seconds,
        claude_max_output_chars=settings.knowledge.claude_max_output_chars,
        max_tool_found_items=settings.knowledge.max_tool_found_items,
        max_tool_item_content_chars=settings.knowledge.max_tool_item_content_chars,
        doc_knowledge_dir=doc_knowledge_dir,
        doc_knowledge_dirs=doc_knowledge_dirs,
        sql_examples_dir=sql_examples_dir,
    )
    registry = ProviderRegistry(settings.providers)
    knowledge_tools = build_knowledge_tools()

    try:
        interactive_session = not args.input_file
        preset_user_input: dict[str, Any] | None = None
        if not interactive_session:
            preset_user_input = _load_user_input(args.input_file)

        while True:
            try:
                if interactive_session:
                    user_input = ui.prompt_user_request()
                    if user_input is None:
                        ui.stop()
                        print("SqlMate session closed.")
                        return 0
                else:
                    user_input = preset_user_input

                # 如果 --resume 指定了 run_id 则复用，否则生成新的时间戳 ID。
                # 若同时使用 --resume 和 --force，先清除已有 checkpoint 再从头开始。
                if args.resume:
                    run_id = args.resume
                else:
                    run_id = _timestamp_run_id()

                # 新日志目录结构：logs/{run_id}/ 而非日志平铺在 logs/ 下
                run_log_dir = log_dir / run_id
                run_log_dir.mkdir(parents=True, exist_ok=True)
                configure_logging(run_log_dir, "run", enable_console=False)
                run_logger = JsonlRunLogger(run_log_dir / "run.jsonl")
                logging.info("Starting SqlMate run_id=%s", run_id)
                ui.reset_for_new_run(user_input["task_goal"])

                if args.ui_preview:
                    _run_ui_preview(ui, user_input)
                    ui.reset_interrupt_state()
                    intr_count = 0
                    if not interactive_session:
                        ui.stop()
                        print("SqlMate UI preview completed.")
                        return 0
                    continue

                # ---- Checkpoint / Resume ----
                checkpoint_manager = CheckpointManager(run_log_dir)
                if args.force:
                    checkpoint_manager.delete_checkpoints()
                    if args.resume:
                        ui.warning(f"Cleared existing checkpoints for run_id={run_id}. Starting fresh.")

                resume_data = None
                if args.resume:
                    completed_stages, restored_planner_output, restored_user_input = (
                        checkpoint_manager.load_checkpoint()
                    )
                    if completed_stages:
                        resume_data = (completed_stages, restored_planner_output)
                        if restored_user_input:
                            user_input = restored_user_input
                        ui.note_output(
                            f"Resuming run_id={run_id} from: {', '.join(sorted(completed_stages))}"
                        )
                    else:
                        ui.warning(f"No checkpoint found for run_id={run_id}. Starting from scratch.")

                # 持久化用户输入，供恢复时复用
                checkpoint_manager.save_user_input(user_input)
                # -----------------------------------

                ctx = SqlMateContext(
                    run_id=run_id,
                    run_logger=run_logger,
                    prompt_loader=prompt_loader,
                    knowledge_service=knowledge_service,
                    output_dir=output_dir,
                    ui=ui,
                    checkpoint_dir=run_log_dir,
                )
                run_config = build_run_config(settings, run_id)
                planner = PlannerPipeline(
                    draft_node=_build_node(
                        "PLANNER_DRAFT",
                        settings,
                        registry,
                        run_config,
                        PlannerDraft,
                        "agent",
                        tools=knowledge_tools,
                        force_first_tool=True,
                    ),
                    critic_node=_build_node("PLANNER_CRITIC", settings, registry, run_config, PlannerReview, "llm"),
                    finalizer_node=_build_node("PLANNER_FINALIZER", settings, registry, run_config, PlannerOutput, "agent", tools=knowledge_tools),
                    revision_node=_build_node("PLANNER_REVISION", settings, registry, run_config, PlannerOutput, "agent", tools=knowledge_tools),
                    phase_refiners={
                        "SETUP": _build_node(
                            "SETUP_PLAN_REFINER",
                            settings,
                            registry,
                            run_config,
                            PhasePlan,
                            "agent",
                            tools=knowledge_tools,
                            force_first_tool=True,
                        ),
                        "DDL": _build_node(
                            "DDL_PLAN_REFINER",
                            settings,
                            registry,
                            run_config,
                            PhasePlan,
                            "agent",
                            tools=knowledge_tools,
                            force_first_tool=True,
                        ),
                        "CORE": _build_node(
                            "CORE_PLAN_REFINER",
                            settings,
                            registry,
                            run_config,
                            PhasePlan,
                            "agent",
                            tools=knowledge_tools,
                            force_first_tool=True,
                        ),
                    },
                    debate_rounds=settings.planner.debate_rounds,
                )
                orchestrator = WorkflowOrchestrator(
                    planner=planner,
                    setup_node=_build_node("SETUP", settings, registry, run_config, PhaseResult, "agent"),
                    ddl_node=_build_node("DDL", settings, registry, run_config, PhaseResult, "agent"),
                    core_node=_build_node("CORE", settings, registry, run_config, PhaseResult, "agent"),
                    output_dir=output_dir,
                    settings=settings,
                    checkpoint_manager=checkpoint_manager,
                )
                result = await orchestrator.run(ctx, user_input, resume_data=resume_data)
                output_path = output_dir / f"{run_id}.json"
                ui.note_output(f"结果已输出到: {output_path}")
                ui.note_output(f"执行结果: {result.execution.message}")
                ui.reset_interrupt_state()
                intr_count = 0

                if not interactive_session:
                    ui.stop()
                    print("SqlMate run completed successfully.")
                    print(f"Output: {output_path}")
                    print(f"Execution: {result.execution.message}")
                    return 0
                if not ui.prompt_continue():
                    ui.stop()
                    print("SqlMate session closed.")
                    return 0
            except (KeyboardInterrupt, WorkflowInterrupted) as exc:
                ui.warning(ui.interrupt_hint(intr_count))
                if intr_count >= 2 or not interactive_session:
                    ui.stop()
                    return 130
                continue
            except asyncio.CancelledError:
                ui.warning(ui.interrupt_hint(intr_count))
                if intr_count >= 2 or not interactive_session:
                    ui.stop()
                    return 130
                continue
    except Exception:
        ui.stop()
        raise
    except asyncio.CancelledError:
        ui.stop()
        return 130
    finally:
        signal.signal(signal.SIGINT, _old_sigint)


def main() -> int:
    """同步入口，负责启动 asyncio 事件循环。"""
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        return 130
    except asyncio.CancelledError:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
