"""Regression tests: queued logging must survive interpreter teardown.

Production incident (hermes1, 2026-08): log records emitted during
interpreter finalization crashed ``_NonFormattingQueueHandler.prepare``
with ``ImportError: sys.meta_path is None, Python is likely shutting
down``. ``copy.copy()`` walks ``__reduce_ex__`` which needs the import
machinery — already torn down at that point. The immediate trigger was
asyncio's default exception handler logging "Task was destroyed but it
is pending!" for leaked NATS client tasks at GC time; every such record
produced a "--- Logging error ---" block plus a copy.py traceback.

Two invariants under test:

1. Normal operation is unchanged — prepare() still returns an isolated
   shallow copy (cross-thread mutation guard, see the class docstring).
2. When the import machinery is gone, prepare()/emit() degrade
   gracefully instead of blowing up the logging machinery.
"""

import logging
import queue
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import hermes_logging
from hermes_logging import _NonFormattingQueueHandler

REPO_ROOT = Path(__file__).resolve().parent.parent


def _record() -> logging.LogRecord:
    return logging.LogRecord("t", logging.WARNING, "x.py", 1, "msg %s", ("a",), None)


def test_prepare_returns_isolated_shallow_copy_normally():
    """The copy is the point (cross-thread mutation race) — keep it."""
    handler = _NonFormattingQueueHandler(queue.SimpleQueue())
    rec = _record()
    out = handler.prepare(rec)
    assert out is not rec
    assert out.msg == rec.msg
    assert out.args == rec.args
    assert out.levelno == rec.levelno


def test_prepare_falls_back_when_copy_raises_import_error(monkeypatch):
    """During finalization copy.copy raises ImportError; prepare must not."""
    def _boom(_obj):
        raise ImportError("sys.meta_path is None, Python is likely shutting down")

    monkeypatch.setattr(
        hermes_logging, "copy", types.SimpleNamespace(copy=_boom)
    )
    handler = _NonFormattingQueueHandler(queue.SimpleQueue())
    rec = _record()
    out = handler.prepare(rec)  # must not raise
    assert out is not None
    assert out.msg == rec.msg


def test_prepare_falls_back_on_any_copy_failure(monkeypatch):
    """Any teardown-stage failure mode (not just ImportError) is swallowed."""
    def _boom(_obj):
        raise AttributeError("module 'copyreg' has been purged")

    monkeypatch.setattr(
        hermes_logging, "copy", types.SimpleNamespace(copy=_boom)
    )
    handler = _NonFormattingQueueHandler(queue.SimpleQueue())
    assert handler.prepare(_record()) is not None


def test_emit_during_simulated_teardown_prints_no_logging_error():
    """End-to-end: real module purge + sys.meta_path=None, real emit().

    Reproduces the exact production failure condition in a subprocess
    (del sys.modules['copyreg'] + sys.meta_path=None is what interpreter
    finalization does before the last GC collects pending asyncio tasks).
    """
    script = textwrap.dedent(
        f"""
        import logging, queue, sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        import hermes_logging

        handler = hermes_logging._NonFormattingQueueHandler(queue.SimpleQueue())
        rec = logging.LogRecord("t", logging.WARNING, "x.py", 1, "late log", (), None)

        # Simulate final interpreter teardown: import machinery torn down,
        # modules purged from sys.modules (this is what Py_FinalizeEx does
        # before the last GC round that collects pending asyncio tasks).
        sys.modules.pop("copyreg", None)
        sys.meta_path = None

        logging.raiseExceptions = True  # production default in dev installs
        handler.emit(rec)  # with the bug: prints '--- Logging error ---'
        sys.stderr.write("EMIT-SURVIVED\\n")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "EMIT-SURVIVED" in proc.stderr, proc.stderr
    assert "Logging error" not in proc.stderr
    assert "meta_path" not in proc.stderr
