"""CrewAI orchestration for the local narrative engine."""
import os
from typing import ClassVar

# Keep the default deployment local, including framework telemetry.
os.environ.setdefault('CREWAI_DISABLE_TELEMETRY', 'true')
os.environ.setdefault('OTEL_SDK_DISABLED', 'true')

from crewai.flow.flow import Flow, listen, router, start


class NarrativeFlow(Flow):
    """Use the game's SQLite history in place of CrewAI's automatic memory."""

    _skip_auto_memory: ClassVar[bool] = True


__all__ = ['NarrativeFlow', 'listen', 'router', 'start']
