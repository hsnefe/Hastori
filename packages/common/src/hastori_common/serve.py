"""uvicorn for services that own their shutdown."""

import contextlib
from collections.abc import Iterator

import uvicorn


class Server(uvicorn.Server):
    """uvicorn without its own signal handling.

    By default uvicorn installs SIGTERM/SIGINT handlers, stops the HTTP server first and
    re-raises the signal afterwards, so /healthz and /metrics are already gone while the queue
    drains. Here the service owns the signals and closes HTTP last.
    """

    def install_signal_handlers(self) -> None:
        return None

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield
