from __future__ import annotations

from pathlib import Path


class PromptLoader:
    """按节点名从磁盘加载 prompt 文件。"""

    def __init__(self, prompts_dir: Path) -> None:
        self.prompts_dir = prompts_dir

    def load(self, node_name: str) -> str:
        """读取指定节点的 `.md` prompt 内容。"""
        prompt_path = self.prompts_dir / f"{node_name}.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
        return prompt_path.read_text(encoding="utf-8").strip()
