"""Load and validate runtime settings. Missing model keys fail loudly; no mock fallback."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    db_path: Path
    host: str
    port: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout: float
    llm_concurrency: int
    log_dir: Path
    prompt_version: str
    narration_max_chars: int
    rules_path: Path
    presets_dir: Path
    prompts_dir: Path
    demos_dir: Path = PROJECT_ROOT / 'config' / 'demos'
    llm_provider: str = "auto"
    llm_json_output: bool = True
    llm_thinking: str = "disabled"
    llm_reasoning_effort: str = "low"
    llm_stream: bool = True
    memory_model_dir: Path | None = None


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        if '=' not in line:
            raise ValueError('.env 行缺少等号')
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if not key.replace('_', 'a').isalnum():
            raise ValueError('.env 变量名不合法')
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} 必须为 true/false 或 1/0")
    return value in {"true", "1"}


def _database_path() -> Path:
    path = Path(os.getenv('DB_PATH', 'data/engine.sqlite3'))
    if path.is_absolute():
        return path
    data_dir = (PROJECT_ROOT / 'data').resolve()
    if path.parts[:1] == ('data',):
        path = path.relative_to('data')
    path = (data_dir / path).resolve()
    if path == data_dir or not path.is_relative_to(data_dir):
        raise ValueError('相对 DB_PATH 必须指向 data/ 下的存档文件')
    return path


def load_settings(env_path: Path | None = None) -> Settings:
    root = PROJECT_ROOT
    load_env(env_path or (root / '.env'))
    return Settings(
        db_path=_database_path(),
        memory_model_dir=(Path(os.environ['MEMORY_MODEL_DIR']).expanduser() if os.getenv('MEMORY_MODEL_DIR')
                          else root / 'data' / 'embedding-model') if _env_bool('MEMORY_VECTOR', True) else None,
        host=os.getenv('HOST', '127.0.0.1'),
        port=int(os.getenv('PORT', '8000')),
        llm_base_url=os.getenv('LLM_BASE_URL', '').rstrip('/'),
        llm_api_key=os.getenv('LLM_API_KEY', ''),
        llm_model=os.getenv('LLM_MODEL', ''),
        llm_timeout=float(os.getenv('LLM_TIMEOUT', '90')),
        llm_concurrency=int(os.getenv('LLM_CONCURRENCY', '4')),
        log_dir=Path(os.getenv('LOG_DIR', 'logs')),
        prompt_version=os.getenv('PROMPT_VERSION', '2026-11-12-v1'),
        narration_max_chars=int(os.getenv('NARRATION_MAX_CHARS', '180')),
        rules_path=root / 'config' / 'rules' / 'four_dice.json',
        presets_dir=root / 'config' / 'presets',
        prompts_dir=root / 'app' / 'prompts',
        demos_dir=root / 'config' / 'demos',
        llm_provider=os.getenv("LLM_PROVIDER", "auto"),
        llm_json_output=_env_bool("LLM_JSON_OUTPUT", True),
        llm_thinking=os.getenv("LLM_THINKING", "disabled"),
        llm_reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "low"),
        llm_stream=_env_bool("LLM_STREAM", True),
    )


def validate_settings(settings: Settings, *, require_model: bool = True) -> None:
    if require_model and not all((settings.llm_base_url, settings.llm_api_key, settings.llm_model)):
        raise ValueError('请配置 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL；运行时不会自动使用 mock')
    if settings.port <= 0 or settings.port > 65535:
        raise ValueError('PORT 非法')
    if settings.llm_concurrency < 1:
        raise ValueError('LLM_CONCURRENCY 必须 >= 1')
    if settings.narration_max_chars < 60:
        raise ValueError('NARRATION_MAX_CHARS 过小')

    if settings.llm_provider not in {"auto", "deepseek", "openai_compatible"}:
        raise ValueError("LLM_PROVIDER 必须为 auto/deepseek/openai_compatible")
    if settings.llm_thinking not in {"enabled", "disabled"}:
        raise ValueError("LLM_THINKING 必须为 enabled/disabled")
    if settings.llm_reasoning_effort not in {"low", "high", "max"}:
        raise ValueError("LLM_REASONING_EFFORT 必须为 low/high/max")
    if settings.llm_timeout <= 0:
        raise ValueError("LLM_TIMEOUT 必须大于 0")
