from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from app.schemas import PlannerOutput

CHECKPOINT_SCHEMA_VERSION = 1
STAGE_ORDER = ["planner", "setup_plan", "ddl_plan", "core_plan"]
STAGE_FILE = "planner_output.json"


class CheckpointManager:
    """管理 SqlMate 工作流的 checkpoint 持久化与恢复。

    在每个用户 approve 的阶段保存 PlannerOutput，支持从最近的
    checkpoint 恢复运行，避免重复执行耗时较长的 LLM 规划调用。

    目录结构::

        logs/{run_id}/
          run.log
          run.jsonl
          user_input.json
          checkpoint_manifest.json
          planner/planner_output.json
          setup_plan/planner_output.json
          ddl_plan/planner_output.json
          core_plan/planner_output.json
    """

    def __init__(self, run_log_dir: Path) -> None:
        self.run_log_dir = run_log_dir

    # ---- 路径属性 ----

    @property
    def manifest_path(self) -> Path:
        return self.run_log_dir / "checkpoint_manifest.json"

    @property
    def user_input_path(self) -> Path:
        return self.run_log_dir / "user_input.json"

    @property
    def pid_path(self) -> Path:
        return self.run_log_dir / ".pid"

    def stage_dir(self, stage: str) -> Path:
        return self.run_log_dir / stage

    # ---- 文件级锁 ----

    def acquire_run_lock(self) -> bool:
        """尝试获取运行锁（PID 文件）。

        如果 .pid 文件已存在且对应进程仍存活，返回 False。
        否则创建/覆盖 .pid 文件并返回 True。
        """
        if self.pid_path.exists():
            try:
                existing_pid = int(self.pid_path.read_text().strip())
                os.kill(existing_pid, 0)
                return False
            except (ValueError, OSError):
                pass
        self.pid_path.write_text(str(os.getpid()))
        return True

    def release_run_lock(self) -> None:
        """释放运行锁。"""
        try:
            self.pid_path.unlink(missing_ok=True)
        except OSError:
            pass

    # ---- 保存 ----

    def save_user_input(self, user_input: dict) -> None:
        """持久化用户原始输入。"""
        self.user_input_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_input_path.write_text(
            json.dumps(user_input, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_planner_output(self, stage: str, planner_output: PlannerOutput) -> None:
        """将 PlannerOutput 保存到指定阶段的 checkpoint 子目录。

        同时更新 checkpoint_manifest.json 记录该阶段完成时间。
        重复保存同一阶段是幂等的——会覆盖之前的内容。
        """
        stage_path = self.stage_dir(stage)
        stage_path.mkdir(parents=True, exist_ok=True)

        output_file = stage_path / STAGE_FILE
        output_file.write_text(
            json.dumps(planner_output.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self._append_manifest(stage)

    # ---- Manifest 操作 ----

    def _read_manifest(self) -> dict:
        """读取 manifest，文件不存在或格式错误时返回空 dict。"""
        if not self.manifest_path.exists():
            return {}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logging.warning("Checkpoint manifest corrupted, treating as empty.")
            return {}
        if not isinstance(data, dict):
            return {}
        version = data.get("schema_version", 0)
        if version != CHECKPOINT_SCHEMA_VERSION:
            logging.warning(
                "Checkpoint schema version mismatch: got %d, expected %d.",
                version,
                CHECKPOINT_SCHEMA_VERSION,
            )
            return {}
        return data

    def _write_manifest(self, data: dict) -> None:
        """写入 manifest JSON 文件。"""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _append_manifest(self, stage: str) -> None:
        """将指定阶段追加到 manifest 的已完成列表。

        如果该阶段已存在于列表中则更新其时间戳，
        否则按 STAGE_ORDER 顺序插入。
        """
        data = self._read_manifest()
        if not data:
            data = {"schema_version": CHECKPOINT_SCHEMA_VERSION, "completed_stages": []}

        completed = data.setdefault("completed_stages", [])
        existing = {entry.get("stage"): entry for entry in completed}

        entry = {
            "stage": stage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if stage in existing:
            existing[stage].update(entry)
        else:
            inserted = False
            for idx, s in enumerate(STAGE_ORDER):
                if s == stage:
                    completed.insert(idx, entry)
                    inserted = True
                    break
            if not inserted:
                completed.append(entry)

        self._write_manifest(data)

    # ---- 加载 ----

    def get_completed_stages(self) -> set[str]:
        """从 manifest 读取已完成的阶段名称集合。"""
        data = self._read_manifest()
        if not data or "completed_stages" not in data:
            return set()
        return {entry["stage"] for entry in data["completed_stages"] if "stage" in entry}

    def load_user_input(self) -> dict:
        """加载用户原始输入，文件不存在返回空 dict。"""
        if not self.user_input_path.exists():
            return {}
        try:
            return json.loads(self.user_input_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logging.warning("User input checkpoint corrupted.")
            return {}

    def load_planner_output(self, stage: str) -> PlannerOutput | None:
        """从指定阶段目录加载 PlannerOutput。"""
        output_file = self.stage_dir(stage) / STAGE_FILE
        if not output_file.exists():
            return None
        try:
            data = json.loads(output_file.read_text(encoding="utf-8"))
            return PlannerOutput.model_validate(data)
        except Exception as exc:
            logging.warning("Failed to load checkpoint for stage %s: %s", stage, exc)
            return None

    def load_checkpoint(self) -> tuple[set[str], PlannerOutput | None, dict]:
        """加载最近一次完成的 checkpoint。

        返回 (completed_stages, planner_output, user_input)。
        如果没有任何 checkpoint，completed_stages 为空 set，
        planner_output 为 None。
        """
        data = self._read_manifest()
        if not data or "completed_stages" not in data:
            return set(), None, {}

        completed_stages_list = data.get("completed_stages", [])
        if not completed_stages_list:
            return set(), None, {}

        completed = {entry["stage"] for entry in completed_stages_list if "stage" in entry}

        # 从最近（最靠后）的阶段开始尝试加载
        for entry in reversed(completed_stages_list):
            stage = entry.get("stage", "")
            if not stage:
                continue
            planner_output = self.load_planner_output(stage)
            if planner_output is not None:
                user_input = self.load_user_input()
                return completed, planner_output, user_input
            logging.warning("Checkpoint stage %s exists in manifest but data is missing.", stage)

        # 所有阶段数据都不可读
        return set(), None, {}

    # ---- 删除 ----

    def delete_checkpoints(self) -> None:
        """删除所有 checkpoint 数据（子目录 + manifest + user_input）。"""
        for stage in STAGE_ORDER:
            stage_path = self.stage_dir(stage)
            if stage_path.exists():
                import shutil
                shutil.rmtree(stage_path)

        for path in (self.manifest_path, self.user_input_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
