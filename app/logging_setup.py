from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def configure_logging(log_dir: Path, run_id: str, enable_console: bool = False) -> Path:
    """初始化标准日志系统，并返回本次运行对应的日志文件路径。

    默认只写文件，不向终端输出，避免破坏 TUI / REPL 的界面布局。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / f"{run_id}.log"

    handlers: list[logging.Handler] = [logging.FileHandler(logfile, encoding="utf-8")]
    if enable_console:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return logfile


class JsonlRunLogger:
    """将运行时事件持续写入 JSONL 文件。

    JSONL 适合后续做：
    1. 逐事件回放
    2. 自动分析
    3. 与最终产物关联排查
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        """追加一条标准化事件记录。"""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
