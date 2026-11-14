"""Load versioned markdown prompt specs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptSpec:
    module: str
    version: str
    text: str


def load(module: str, version: str, prompts_dir: Path) -> PromptSpec:
    path = prompts_dir / f'{module}.md'
    if not path.exists():
        raise FileNotFoundError(f'缺少提示词文件: {path}')
    return PromptSpec(module=module, version=version, text=path.read_text(encoding='utf-8'))
