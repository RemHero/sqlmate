from __future__ import annotations

import re

from app.schemas import PhaseOutline, PhasePlan, PlannerDraft, PlannerOutput


def _extract_contract_objects(required_setup_objects: list[dict[str, str]]) -> tuple[list[str], list[dict[str, str]]]:
    databases: list[str] = []
    tables: list[dict[str, str]] = []
    for obj in required_setup_objects or []:
        obj_type = str(obj.get("type", "")).strip().lower()
        name = str(obj.get("name") or obj.get("table_name") or "").strip()
        if not name:
            continue
        if obj_type == "database":
            databases.append(name)
        elif obj_type == "table" or obj.get("table_name") or not obj_type:
            tables.append(obj)
    return databases, tables


def _sql_mentions_name(sql: str, name: str) -> bool:
    pattern = re.compile(rf"(?i)(^|[^a-zA-Z0-9_])`?{re.escape(name)}`?([^a-zA-Z0-9_]|$)")
    return bool(pattern.search(sql))


def _extract_created_tables(sql: str) -> list[str]:
    pattern = re.compile(
        r"(?is)CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([`\"\w\.]+)"
    )
    names: list[str] = []
    for raw_name in pattern.findall(sql or ""):
        cleaned = raw_name.strip().strip("`\"")
        if "." in cleaned:
            cleaned = cleaned.split(".")[-1]
        names.append(cleaned)
    return names


def _validate_phase_outline(name: str, outline: PhaseOutline) -> list[str]:
    issues: list[str] = []
    if not outline.objective.strip():
        issues.append(f"{name}.objective must not be empty")
    return issues


def validate_phase_plan(plan: PhasePlan, label: str = "phase_plan") -> list[str]:
    issues: list[str] = []
    if not plan.objective.strip():
        issues.append(f"{label}.objective must not be empty")
    return issues


def validate_planner_draft(draft: PlannerDraft) -> list[str]:
    issues: list[str] = []
    global_plan = draft.global_plan

    if not global_plan.task_goal.strip():
        issues.append("global_plan.task_goal must not be empty")
    if not global_plan.feature_under_test.strip():
        issues.append("global_plan.feature_under_test must not be empty")

    for phase_name, phase_outline in [
        ("setup_outline", draft.setup_outline),
        ("ddl_outline", draft.ddl_outline),
        ("core_outline", draft.core_outline),
    ]:
        issues.extend(_validate_phase_outline(phase_name, phase_outline))
    return issues


def validate_planner_output(output: PlannerOutput) -> list[str]:
    issues = validate_planner_draft(
        PlannerDraft(
            global_plan=output.global_plan,
            setup_outline=output.setup_outline,
            ddl_outline=output.ddl_outline,
            core_outline=output.core_outline,
            planner_notes=[],
        )
    )
    for label, plan in [
        ("setup_plan", output.setup_plan),
        ("ddl_plan", output.ddl_plan),
        ("core_plan", output.core_plan),
    ]:
        if plan is not None:
            issues.extend(validate_phase_plan(plan, label))
    return issues


def validate_phase_contract(
    stage: str,
    generated_sql: str,
    planner_output: PlannerOutput,
    plan: PhasePlan,
    ddl_generated_sql: str | None = None,
) -> list[str]:
    issues: list[str] = []
    sql = generated_sql or ""
    stage_upper = stage.upper()
    databases, tables = _extract_contract_objects(planner_output.global_plan.required_setup_objects)

    if stage_upper == "SETUP":
        return issues

    if stage_upper == "DDL":
        for table in tables:
            table_name = str(table.get("name") or table.get("table_name") or "").strip()
            if table_name and not _sql_mentions_name(sql, table_name):
                issues.append(f"DDL does not reference planned table {table_name}")
            columns = str(table.get("columns", "")).strip()
            if columns:
                for column_def in [part.strip() for part in columns.split(",") if part.strip()]:
                    column_name = column_def.split()[0]
                    if column_name and not _sql_mentions_name(sql, column_name):
                        issues.append(f"DDL does not include planned column {column_name}")
        return issues

    if stage_upper == "CORE":
        ddl_tables = set(_extract_created_tables(ddl_generated_sql or ""))
        planned_table_names = [
            str(table.get("name") or table.get("table_name") or "").strip()
            for table in tables
            if str(table.get("name") or table.get("table_name") or "").strip()
        ]
        for table_name in planned_table_names:
            if not _sql_mentions_name(sql, table_name):
                issues.append(f"CORE does not reference planned table {table_name}")
            if ddl_generated_sql and ddl_tables and table_name not in ddl_tables:
                issues.append(f"DDL output did not create planned table {table_name}")
        if ddl_generated_sql and ddl_tables:
            if not any(_sql_mentions_name(sql, table_name) for table_name in ddl_tables):
                issues.append("CORE does not reference any table created by DDL output")
        return issues

    return issues


def validate_phase_sql(stage: str, generated_sql: str) -> list[str]:
    """对阶段 SQL 做一层轻量级结构校验。

    目标不是替代真实数据库执行，而是尽早拦截几类很常见的坏结果：
    1. 空 SQL
    2. DDL 中明显损坏的 CREATE TABLE 结构
    3. SETUP 中缺少基本环境初始化语句
    """
    issues: list[str] = []
    sql = generated_sql or ""
    upper_sql = sql.upper()
    stage_upper = stage.upper()

    if not sql.strip():
        return ["generated_sql is empty"]

    if stage_upper == "SETUP":
        if "CREATE TABLE" in upper_sql or "PRIMARY KEY" in upper_sql:
            issues.append("SETUP should not generate table-definition SQL")
        if " FORCE" in upper_sql:
            issues.append("SETUP contains non-portable FORCE clause")
        return issues

    if stage_upper == "DDL":
        if not any(keyword in upper_sql for keyword in ["CREATE TABLE", "CREATE VIEW", "ALTER TABLE", "CREATE INDEX"]):
            issues.append("DDL missing recognizable DDL statement")
        if sql.count("(") != sql.count(")"):
            issues.append("DDL has unbalanced parentheses")
        return issues

    if stage_upper == "CORE":
        if "SELECT" not in upper_sql:
            issues.append("CORE missing validation SELECT")
        return issues

    return issues
