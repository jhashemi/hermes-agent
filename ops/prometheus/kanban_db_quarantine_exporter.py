#!/usr/bin/env python3
"""kanban-db-quarantine Prometheus exporter (VFE-KANBAN-CORRUPTION-02, t_09bf9d6c).

Imports ``hermes_cli.kanban_db`` so the default prometheus_client REGISTRY
gets the ``hermes_kanban_db_corrupt_quarantine_total{board=...}`` counter
registered into it, then serves ``/metrics`` on :9485 so Prometheus can scrape
the live value from any running kanban worker on this host.

Design notes
------------
* Uses the default prometheus_client REGISTRY (not a custom CollectorRegistry),
  so the counter registered by the import of ``hermes_cli.kanban_db`` is visible
  here automatically — no coupling code needed.
* A periodic "freshness sweep" reads ``.corrupt.*.bak`` files on disk and calls
  the same ``_observe_corrupt_quarantine(board)`` funnel that the quarantine code
  paths call.  This means:
  - If this exporter restarts, it replays any quarantine events that landed
    *before* it started, so the counter does not drop to 0 on restart.
  - It is the canonical "catch-all" for any quarantine event missed because the
    spawning process did not import hermes_cli.kanban_db with prometheus_client
    available (e.g. a bare Python subprocess without the venv).
* The freshness sweep is idempotent because prometheus_client counters are
  monotonically increasing within a process lifetime; they do not reset.  Between
  restarts the counter resets to the on-disk count, which is correct.

Deployment
----------
Install as a user-scoped systemd service:

    sudo cp ops/prometheus/kanban_db_quarantine_exporter.py \\
             /home/ubuntu/executive_agents_platform/ops/grafana/
    systemctl --user daemon-reload
    systemctl --user enable --now kanban-db-quarantine-exporter.service

Then add a scrape job in /etc/prometheus/prometheus.yml (handled by the task's
prometheus.yml patch):

    - job_name: 'kanban-db-quarantine'
      static_configs:
        - targets: ['localhost:9485']

Environment variables
---------------------
KANBAN_HOME        default: ~/.hermes/kanban/boards/
EXPORTER_PORT      default: 9485
SWEEP_INTERVAL     default: 30  (seconds)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Optional

logger = logging.getLogger("kanban_db_quarantine_exporter")

# ---------------------------------------------------------------------------
# Lazy-import the hermes_cli package so the quarantine counter registers on
# the default prometheus_client REGISTRY before we serve /metrics.
# ---------------------------------------------------------------------------
try:
    # This import has a side-effect: it registers
    #   hermes_kanban_db_corrupt_quarantine_total
    # on the default prometheus_client REGISTRY (import-guarded inside kanban_db).
    import hermes_cli.kanban_db as _kanban_db  # noqa: F401 — side-effect import
    from hermes_cli.kanban_db import _observe_corrupt_quarantine  # type: ignore
    _KANBAN_DB_AVAILABLE = True
    logger.info("hermes_cli.kanban_db imported — quarantine counter registered")
except Exception as exc:  # pragma: no cover
    logger.warning("hermes_cli.kanban_db unavailable (%s) — counter will be absent", exc)
    _KANBAN_DB_AVAILABLE = False
    _observe_corrupt_quarantine = None  # type: ignore

_REGISTRY = None
_generate_latest = None
try:
    from prometheus_client import REGISTRY as _REGISTRY, generate_latest as _generate_latest  # type: ignore
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROM_AVAILABLE = False
    logger.error("prometheus_client not installed — /metrics will return 503")


# ---------------------------------------------------------------------------
# Track which .corrupt.*.bak files we have already replayed so we don't
# double-count across sweep iterations within a single process lifetime.
# ---------------------------------------------------------------------------
_REPLAYED: set[str] = set()


def _sweep_quarantine_artefacts(kanban_home: Path) -> int:
    """Scan kanban boards for .corrupt.*.bak artefacts and replay counters.

    Returns the number of new artefacts observed this sweep.
    """
    if not _KANBAN_DB_AVAILABLE or _observe_corrupt_quarantine is None:
        return 0

    new_count = 0
    try:
        for bak_file in kanban_home.rglob("*.corrupt.*.bak"):
            key = str(bak_file)
            if key in _REPLAYED:
                continue
            # Board slug is the immediate parent directory of kanban.db,
            # and the .bak lives alongside kanban.db in that same dir.
            board = bak_file.parent.name
            try:
                _observe_corrupt_quarantine(board)
                _REPLAYED.add(key)
                new_count += 1
                logger.info(
                    "replayed quarantine artefact board=%s file=%s", board, bak_file.name
                )
            except Exception as exc:  # pragma: no cover
                logger.debug("replay failed for %s: %s", bak_file, exc)
    except Exception as exc:  # pragma: no cover
        logger.debug("sweep error: %s", exc)
    return new_count


def _sweep_loop(kanban_home: Path, interval: int) -> None:
    """Background thread: sweep every ``interval`` seconds."""
    while True:
        try:
            n = _sweep_quarantine_artefacts(kanban_home)
            if n:
                logger.info("sweep found %d new quarantine artefacts", n)
        except Exception as exc:  # pragma: no cover
            logger.debug("sweep_loop error: %s", exc)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class _MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # silence access log
        pass

    def do_GET(self) -> None:
        if self.path not in ("/metrics", "/metrics/"):
            self.send_response(404)
            self.end_headers()
            return

        if not _PROM_AVAILABLE:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"prometheus_client not installed\n")
            return

        try:
            body = _generate_latest(_REGISTRY)  # type: ignore[misc]
        except Exception as exc:  # pragma: no cover
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"generate_latest error: {exc}\n".encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        if self.path in ("/metrics", "/metrics/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Kanban DB quarantine Prometheus exporter"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("EXPORTER_PORT", "9485")),
        help="HTTP port to expose /metrics on (default: 9485)",
    )
    parser.add_argument(
        "--kanban-home",
        type=Path,
        default=Path(
            os.environ.get("KANBAN_HOME",
                           "/home/ubuntu/.hermes/kanban/boards")
        ),
        help="Root directory containing per-board kanban subdirs",
    )
    parser.add_argument(
        "--sweep-interval",
        type=int,
        default=int(os.environ.get("SWEEP_INTERVAL", "30")),
        help="Seconds between disk sweeps for .corrupt.*.bak artefacts (default: 30)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    kanban_home: Path = args.kanban_home
    if not kanban_home.exists():
        logger.warning("kanban_home %s does not exist — disk sweep will be a no-op", kanban_home)

    # Initial sweep before first scrape
    n = _sweep_quarantine_artefacts(kanban_home)
    logger.info("initial sweep: found %d quarantine artefact(s) in %s", n, kanban_home)

    # Background sweep thread
    sweeper = Thread(
        target=_sweep_loop,
        args=(kanban_home, args.sweep_interval),
        daemon=True,
        name="quarantine-sweeper",
    )
    sweeper.start()

    server = HTTPServer(("0.0.0.0", args.port), _MetricsHandler)
    logger.info(
        "kanban-db-quarantine exporter listening on :%d — "
        "scrape at http://localhost:%d/metrics",
        args.port, args.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
