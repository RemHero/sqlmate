from __future__ import annotations

from app.schemas import ExecutionResult


class MockSqlExecutor:
    """用于本地 smoke test 的假执行器。

    当前不连接真实数据库，只验证“是否有 SQL 内容产出”，
    以便先把主流程、日志和产物链路跑通。
    """

    def execute(self, merged_sql: str) -> ExecutionResult:
        """执行一次 mock 校验，并返回统一的执行结果结构。"""
        if not merged_sql.strip():
            return ExecutionResult(
                success=False,
                executor_type="mock",
                message="Merged SQL is empty.",
                failed_stage="merge",
                errors=["No SQL content was produced."],
            )
        return ExecutionResult(
            success=True,
            executor_type="mock",
            message="Mock executor accepted the SQL bundle.",
        )
