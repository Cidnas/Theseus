"""Terminal-only debug rendering for the learning-agent example."""

from __future__ import annotations

import itertools
import sys
import threading
import time
from types import TracebackType
from typing import Any, TextIO


class DebugRenderer:
    """Render progress callbacks without leaking terminal concerns into the core."""

    _FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, enabled: bool, stream: TextIO | None = None) -> None:
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self._interactive = enabled and self.stream.isatty()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._message: str | None = None
        self._started = 0.0
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._event_count = 0
        self._last_activity = 0.0
        self._last_detail = "waiting for first backend event"

    def __enter__(self) -> DebugRenderer:
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def phase(self, message: str) -> None:
        """Complete the prior phase and begin rendering a busy phase."""

        if not self.enabled:
            return
        self._finish_active()
        if not self._interactive:
            self._write(f"[debug] {message}\n")
            return
        self._message = message
        self._started = time.monotonic()
        with self._state_lock:
            self._event_count = 0
            self._last_activity = 0.0
            self._last_detail = "waiting for first backend event"
        self._stop.clear()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def note(self, message: str) -> None:
        """Render a stationary debug message, suitable before user input."""

        if not self.enabled:
            return
        self._finish_active()
        self._write(f"[debug] {message}\n")

    def idle(self) -> None:
        """Stop animation before normal CLI output or input."""

        if self.enabled:
            self._finish_active()

    def model_event(self, role: str, event: dict[str, Any]) -> None:
        """Record model/backend activity for the currently animated phase."""

        if not self.enabled:
            return
        method = event.get("method")
        detail = str(method) if method else "protocol response"
        params = event.get("params")
        if method == "item/tool/call" and isinstance(params, dict):
            detail = f"{role} tool: {params.get('tool', 'unknown')}"
        elif method == "item/completed" and isinstance(params, dict):
            item = params.get("item")
            if isinstance(item, dict):
                detail = f"{role} completed {item.get('type', 'item')}"
        elif method:
            detail = f"{role}: {method}"
        with self._state_lock:
            self._event_count += 1
            self._last_activity = time.monotonic()
            self._last_detail = detail

    def close(self) -> None:
        self.idle()

    def _animate(self) -> None:
        frames = itertools.cycle(self._FRAMES)
        while not self._stop.is_set():
            now = time.monotonic()
            elapsed = int(now - self._started)
            with self._state_lock:
                event_count = self._event_count
                last_activity = self._last_activity
                last_detail = self._last_detail
            if last_activity:
                quiet = int(now - last_activity)
                activity = f"{event_count} events; last {quiet}s ago; {last_detail}"
                if quiet >= 60:
                    activity = f"QUIET {quiet}s; process alive; {last_detail}"
            else:
                activity = last_detail
            self._write(
                f"\r\x1b[2K{next(frames)} {self._message} "
                f"[{elapsed}s | {activity}]"
            )
            self._stop.wait(0.1)

    def _finish_active(self) -> None:
        thread = self._thread
        message = self._message
        if thread is None or message is None:
            return
        self._stop.set()
        thread.join(timeout=1)
        elapsed = int(time.monotonic() - self._started)
        self._write(f"\r\x1b[2K✓ {message} [{elapsed}s]\n")
        self._thread = None
        self._message = None

    def _write(self, value: str) -> None:
        with self._write_lock:
            self.stream.write(value)
            self.stream.flush()
