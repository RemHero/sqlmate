from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.services.run_context import SqlMateContext


class BaseNode(ABC):
    """所有节点的抽象基类。"""

    def __init__(self, node_name: str) -> None:
        self.node_name = node_name

    @abstractmethod
    async def run(self, ctx: SqlMateContext, payload: dict[str, Any]) -> Any:
        """执行节点主逻辑。子类必须实现。"""
        raise NotImplementedError
