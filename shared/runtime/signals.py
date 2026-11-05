from __future__ import annotations

import signal
from contextlib import contextmanager
from types import FrameType


def _interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


@contextmanager
def interrupt_signals():
    """Turn supervisor termination requests into normal finally cleanup."""
    watched = [signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        watched.append(signal.SIGBREAK)
    previous = {sig: signal.signal(sig, _interrupt) for sig in watched}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
