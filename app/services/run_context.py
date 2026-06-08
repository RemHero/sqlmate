from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging_setup import JsonlRunLogger
from app.services.knowledge import KnowledgeService
from app.services.prompt_loader import PromptLoader


@dataclass
class SqlMateContext:
    """整个工作流共享的运行时上下文。

    这个对象会在节点之间传递，保存：
    1. 当前运行 ID
    2. 日志记录器
    3. Prompt 加载器
    4. 知识服务
    5. 输出目录
    6. 共享状态缓存
    7. CLI 交互对象
    """

    run_id: str
    run_logger: JsonlRunLogger
    prompt_loader: PromptLoader
    knowledge_service: KnowledgeService
    output_dir: Path
    shared_state: dict[str, Any] = field(default_factory=dict)
    ui: Any | None = None
