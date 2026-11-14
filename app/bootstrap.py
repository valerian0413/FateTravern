"""Build and shut down the single-process runtime."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.application.chat import ChatService
from app.application.memory_search import MemoryIndex
from app.config import Settings, validate_settings
from app.jobs.worker import Worker
from app.providers.chat_completions import ChatCompletionsProvider
from app.providers.trace import Trace
from app.storage.database import Database


@dataclass
class Runtime:
    settings: Settings
    db: Database
    provider: ChatCompletionsProvider | None
    trace: Trace
    chat: ChatService
    worker: Worker
    fixtures: dict = field(default_factory=dict)
    streams: dict = field(default_factory=dict)
    memory_index: MemoryIndex | None = None


async def build_runtime(settings: Settings, *, require_model: bool = True, fixtures: dict | None = None) -> Runtime:
    validate_settings(settings, require_model=require_model)
    db = Database(settings.db_path)
    await db.connect()
    await db.migrate()
    trace = Trace(log_dir=settings.log_dir)
    provider = None
    if require_model:
        provider = ChatCompletionsProvider(settings, trace)
    runtime = Runtime(
        settings=settings,
        db=db,
        provider=provider,
        trace=trace,
        chat=None,  # type: ignore[arg-type]
        worker=None,  # type: ignore[arg-type]
        fixtures=fixtures or {},
    )
    model_path = settings.memory_model_dir
    if model_path and all((model_path / name).is_file() for name in ('model.onnx', 'tokenizer.json')):
        runtime.memory_index = MemoryIndex(settings.db_path.with_suffix('.vectors'), model_path)
    runtime.chat = ChatService(runtime)
    runtime.worker = Worker(runtime)
    runtime.worker.start()
    return runtime


async def shutdown(runtime: Runtime) -> None:
    await runtime.chat.close()
    await runtime.worker.stop()
    if runtime.provider:
        await runtime.provider.close()
    await runtime.db.close()
