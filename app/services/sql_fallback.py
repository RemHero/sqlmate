from __future__ import annotations

from app.schemas import KnowledgeBundle, PhasePlan, PhaseResult, PlannerOutput


def _resolve_contract_objects(planner_output: PlannerOutput) -> tuple[str, dict[str, str]]:
    database_name = "test_regression_ddl_dml"
    table_contract: dict[str, str] = {
        "name": "t_smoke_feature",
        "columns": "id INT NOT NULL, name VARCHAR(50), value INT, create_time DATETIME",
        "primary_key": "id",
    }
    for obj in planner_output.global_plan.required_setup_objects:
        obj_type = str(obj.get("type", "")).strip().lower()
        if obj_type == "database" and str(obj.get("name", "")).strip():
            database_name = str(obj["name"]).strip()
        elif obj_type == "table" and str(obj.get("name", "")).strip():
            table_contract = {
                "name": str(obj["name"]).strip(),
                "columns": str(obj.get("columns", table_contract["columns"])).strip(),
                "primary_key": str(obj.get("primary_key", table_contract["primary_key"])).strip(),
            }
    return database_name, table_contract


def build_fallback_phase_result(stage: str, planner_output: PlannerOutput, plan: PhasePlan) -> PhaseResult:
    """在远程模型阶段超时或失败时，生成一份确定性的本地 SQL 结果。

    目标不是替代高质量模型生成，而是保证工具在最差情况下仍能：
    1. 给出可执行的基础 SQL
    2. 保持三个阶段之间的对象契约一致
    3. 确保整个 workflow 可以完整跑完并产生产物
    """
    db_name, table_contract = _resolve_contract_objects(planner_output)
    table_name = table_contract["name"]
    column_lines = [part.strip() for part in table_contract["columns"].split(",") if part.strip()]
    primary_key = table_contract["primary_key"] or "id"

    if stage.upper() == "SETUP":
        sql = "\n".join(
            [
                f"DROP DATABASE IF EXISTS {db_name};",
                f"CREATE DATABASE {db_name};",
                f"USE {db_name};",
            ]
        )
        notes = ["Fallback SQL used for SETUP after remote timeout/error."]
        checklist = ["数据库初始化 SQL 使用本地确定性模板生成。"]
    elif stage.upper() == "DDL":
        sql = "\n".join(
            [
                f"USE {db_name};",
                f"DROP TABLE IF EXISTS {table_name};",
                f"CREATE TABLE IF NOT EXISTS {table_name} (",
                *[f"    {line}{',' if index < len(column_lines) - 1 else ''}" for index, line in enumerate(column_lines)],
                ") ENGINE=OLAP",
                f"PRIMARY KEY ({primary_key})",
                f"DISTRIBUTED BY HASH({primary_key}) BUCKETS 8",
                'PROPERTIES ("replication_num" = "1");',
                f"DESC {table_name};",
            ]
        )
        notes = ["Fallback SQL used for DDL after remote timeout/error."]
        checklist = [f"固定使用 {table_name} 与 PRIMARY KEY({primary_key}) 契约。"]
    else:
        sql = "\n".join(
            [
                f"USE {db_name};",
                f"INSERT INTO {table_name} (id, name, value, create_time) VALUES",
                "    (1, 'alpha', 10, NOW()),",
                "    (2, 'beta', 20, NOW()),",
                "    (3, 'gamma', 30, NOW());",
                f"SELECT COUNT(*) AS row_count FROM {table_name};",
                f"SELECT name, value FROM {table_name} WHERE id = 1;",
                f"INSERT INTO {table_name} (id, name, value, create_time) VALUES (1, 'dup', 99, NOW());",
                f"DROP TABLE IF EXISTS {table_name};",
                f"DROP DATABASE IF EXISTS {db_name};",
            ]
        )
        notes = ["Fallback SQL used for CORE after remote timeout/error."]
        checklist = [f"异常路径基于 PRIMARY KEY({primary_key}) 重复键冲突。", "清理语句已包含表和数据库删除。"]

    return PhaseResult(
        stage=stage.upper(),
        plan_summary=f"{stage.upper()} fallback plan execution result.",
        knowledge_used=planner_output.cached_knowledge.get(stage.lower(), KnowledgeBundle(stage=stage.lower())),
        generated_sql=sql,
        validation_notes=notes,
        unresolved_risks=["Fallback SQL was used because the remote node was too slow or failed."],
        evidence_checklist=checklist,
    )
