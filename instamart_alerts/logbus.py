"""In-process log bus: a bounded history plus a fan-out for the web console.

The control panel's console is the only view most runs get, so every log record
this process emits is mirrored into a deque and pushed to whichever browser tabs
are listening on the SSE stream. Events that are not log lines — a poll that
finished, the scheduler flipping state — ride the same channel, so the UI has a
single stream to react to instead of polling three endpoints.

History is also appended to `data/console.log` and re-read on boot, so restarting
the server does not blank the console.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from itertools import count
from pathlib import Path
from typing import Any

MAX_HISTORY = 1500
MAX_RUNS = 60
# Per-subscriber backlog. A tab that stops reading (laptop asleep) is dropped
# rather than allowed to pin the whole history in memory.
SUBSCRIBER_QUEUE = 400
MAX_LOG_BYTES = 4_000_000

_lock = threading.Lock()
_history: list[dict[str, Any]] = []
_runs: list[dict[str, Any]] = []
_subscribers: set[queue.Queue] = set()
_seq = count(1)
_log_path: Path | None = None
_installed = False


def _now() -> float:
    return time.time()


def publish(event: dict[str, Any]) -> dict[str, Any]:
    """Fan an event out to every listener and remember it."""
    event = {"seq": next(_seq), "ts": event.get("ts") or _now(), **event}
    with _lock:
        if event.get("type") == "log":
            _history.append(event)
            del _history[:-MAX_HISTORY]
            _append_to_file(event)
        elif event.get("type") == "run":
            _runs.append(event)
            del _runs[:-MAX_RUNS]
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)
    return event


def log_line(level: str, message: str, *, source: str = "panel") -> dict[str, Any]:
    """Emit a console line that did not come from the logging module."""
    return publish(
        {"type": "log", "level": level.upper(), "logger": source, "message": message}
    )


def history(limit: int = MAX_HISTORY) -> list[dict[str, Any]]:
    with _lock:
        return _history[-limit:]


def runs(limit: int = MAX_RUNS) -> list[dict[str, Any]]:
    with _lock:
        return _runs[-limit:]


def clear() -> None:
    with _lock:
        _history.clear()
        if _log_path is not None:
            try:
                _log_path.write_text("")
            except OSError:
                pass
    log_line("INFO", "console cleared")


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        _subscribers.discard(q)


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)


class BusHandler(logging.Handler):
    """Mirrors the standard logging pipeline onto the bus."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + self.format_exception(record)
        except Exception:  # noqa: BLE001 — a broken log must not kill the caller
            self.handleError(record)
            return
        publish(
            {
                "type": "log",
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
        )

    def format_exception(self, record: logging.LogRecord) -> str:
        import traceback

        return "".join(traceback.format_exception(*record.exc_info)).rstrip()


def _append_to_file(event: dict[str, Any]) -> None:
    """Caller holds the lock."""
    if _log_path is None:
        return
    try:
        with _log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
        if _log_path.stat().st_size > MAX_LOG_BYTES:
            kept = _log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            _log_path.write_text("\n".join(kept[len(kept) // 2 :]) + "\n")
    except OSError:
        pass


def _load_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    restored: list[dict[str, Any]] = []
    for line in lines[-MAX_HISTORY:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "log":
            event["seq"] = next(_seq)
            event["replayed"] = True
            restored.append(event)
    with _lock:
        _history.extend(restored)
        del _history[:-MAX_HISTORY]


def install(data_dir: Path | None = None, level: int = logging.INFO) -> None:
    """Attach the bus to the root logger. Safe to call more than once."""
    global _installed, _log_path

    if data_dir is not None and _log_path is None:
        _log_path = data_dir / "console.log"
        _load_file(_log_path)

    if _installed:
        return
    handler = BusHandler()
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > level:
        root.setLevel(level)
    _installed = True
