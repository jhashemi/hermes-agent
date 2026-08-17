"""Convene-worker: lightweight HTTP bridge from kanban to the boardroom driver.

When a convene-type task is dispatched, the dispatcher calls
``run_convene_worker(task)`` instead of spawning ``hermes -p <assignee> chat``.
This module:

1. Reads the ``convene_spec`` JSON from the task row.
2. POSTs the spec to the boardroom driver (default ``http://localhost:8196/convene``).
3. Polls ``GET /room/<room_id>`` until the deliberation completes (or times out).
4. Saves the transcript to the path named in the spec.
5. Parses the transcript's ``child_tickets`` section (if present) and emits
   child kanban tickets via ``create_task``.
6. Completes the task with a structured summary.

No LLM cost — the worker is pure HTTP + JSON. The boardroom driver itself
calls the LLM backends (Ollama Cloud / Bedrock) for each persona's phase.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Polling defaults. The boardroom driver's deliberation typically takes
# 30-120 seconds (one LLM call per phase, 6-12 phases). We poll every 5s
# with a generous cap so a slow Bedrock round doesn't time out prematurely.
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 600  # 10 minutes

# Child ticket defaults. The convene-worker creates child tickets for
# each entry in the transcript's ``child_tickets`` array. Each child
# needs an assignee — if the transcript doesn't name one, we leave the
# ticket in triage so a human or the decomposer can route it.
DEFAULT_CHILD_INITIAL_STATUS = "triage"


def _resolve_driver_url() -> str:
    """Resolve the boardroom driver URL.

    Priority: ``HERMES_CONVENE_DRIVER_URL`` env var > ``kanban.convene_driver_url``
    config key > ``CONVENE_DRIVER_DEFAULT_URL`` constant.
    """
    env_url = os.environ.get("HERMES_CONVENE_DRIVER_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        cfg_url = kanban_cfg.get("convene_driver_url", "") if isinstance(kanban_cfg, dict) else ""
        if cfg_url and isinstance(cfg_url, str) and cfg_url.strip():
            return cfg_url.strip().rstrip("/")
    except Exception:
        pass
    from hermes_cli.kanban_db import CONVENE_DRIVER_DEFAULT_URL
    return CONVENE_DRIVER_DEFAULT_URL


def _post_convene(driver_url: str, spec: dict) -> dict:
    """POST the convene spec to the boardroom driver.

    Returns the driver's response dict (typically ``{room_id, status}``).
    """
    body = json.dumps(spec).encode("utf-8")
    req = urllib.request.Request(
        f"{driver_url}/convene",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}", "body": err_body}
    except Exception as e:
        return {"error": str(e)}


def _poll_room(driver_url: str, room_id: str, timeout_s: int = POLL_TIMEOUT_S) -> dict:
    """Poll ``GET /room/<room_id>`` until status is 'completed' or 'failed'.

    Returns the final room state dict.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        try:
            req = urllib.request.Request(f"{driver_url}/room/{room_id}")
            with urllib.request.urlopen(req, timeout=15) as resp:
                state = json.loads(resp.read())
            status = state.get("status", "")
            if status in ("completed", "failed"):
                return state
        except Exception as e:
            logger.debug("[convene-worker] poll error for %s: %s", room_id, e)
    return {"error": "timeout", "room_id": room_id}


def _parse_child_tickets(transcript_state: dict) -> list[dict]:
    """Extract child ticket specs from a completed transcript.

    The boardroom driver's transcript may include a ``child_tickets`` key
    (a list of dicts with at minimum ``title`` and optionally ``assignee``,
    ``body``, ``priority``). If absent, returns an empty list.
    """
    children = transcript_state.get("child_tickets")
    if not children or not isinstance(children, list):
        return []
    result: list[dict] = []
    for ct in children:
        if not isinstance(ct, dict):
            continue
        title = ct.get("title", "")
        if not title or not str(title).strip():
            continue
        result.append(ct)
    return result


def _emit_child_tickets(
    conn,
    parent_task_id: str,
    children: list[dict],
    *,
    board: Optional[str] = None,
) -> list[str]:
    """Create child kanban tickets from the transcript's child_tickets.

    Returns a list of created task ids. Each child links to the parent
    convene task via ``parents=[parent_task_id]``.
    """
    from hermes_cli import kanban_db as kb

    created_ids: list[str] = []
    for ct in children:
        title = str(ct["title"]).strip()
        assignee = ct.get("assignee")
        body = ct.get("body", "")
        priority = ct.get("priority", 0)
        # If the transcript names a specific persona as assignee, use it.
        # Otherwise leave in triage for human/decomposer routing. We use
        # the ``triage=True`` flag (not initial_status) since the DB only
        # allows 'blocked' and 'running' as explicit initial statuses.
        try:
            child_id = kb.create_task(
                conn,
                title=title,
                body=body,
                assignee=str(assignee) if assignee else None,
                parents=(parent_task_id,),
                priority=int(priority) if priority is not None else 0,
                triage=bool(not assignee),
                created_by="convene-worker",
            )
            created_ids.append(child_id)
            logger.info(
                "[convene-worker] emitted child ticket %s: %s", child_id, title
            )
        except Exception as e:
            logger.warning(
                "[convene-worker] failed to create child ticket %r: %s",
                title, e,
            )
    return created_ids


def run_convene_worker(
    task_id: str,
    *,
    board: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Execute a convene-type task.

    This is the entry point called by the dispatcher for convene tickets.
    Returns a dict with ``status`` (completed/failed), ``summary``, and
    ``child_tickets`` (list of created ids).
    """
    from hermes_cli import kanban_db as kb

    # Connect to the kanban DB.
    if db_path:
        conn = kb.connect(db_path=db_path, board=board)
    else:
        conn = kb.connect(board=board)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            return {"status": "failed", "error": f"task {task_id} not found"}
        if not task.convene_spec:
            return {
                "status": "failed",
                "error": f"task {task_id} has no convene_spec",
            }
        # Parse the convene spec.
        try:
            spec = json.loads(task.convene_spec)
        except json.JSONDecodeError as e:
            return {
                "status": "failed",
                "error": f"convene_spec is not valid JSON: {e}",
            }
        room_id = spec.get("room_id", task_id)
        transcript_path = spec.get("transcript_output_path", "")
        driver_url = _resolve_driver_url()

        logger.info(
            "[convene-worker] starting convene %s for task %s (driver=%s)",
            room_id, task_id, driver_url,
        )

        # Heartbeat: tell the board we're alive.
        kb.heartbeat_worker(conn, task_id, note=f"convene {room_id}: POSTing to driver")

        # 1. POST the spec to the boardroom driver.
        post_result = _post_convene(driver_url, spec)
        if "error" in post_result:
            return {
                "status": "failed",
                "error": f"convene POST failed: {post_result['error']}",
                "detail": post_result.get("body", ""),
            }

        # 2. Poll for completion.
        kb.heartbeat_worker(
            conn, task_id, note=f"convene {room_id}: polling for completion"
        )
        final_state = _poll_room(driver_url, room_id)
        if "error" in final_state:
            return {
                "status": "failed",
                "error": f"convene poll failed: {final_state['error']}",
            }

        # 3. Save the transcript.
        if transcript_path:
            try:
                import pathlib
                p = pathlib.Path(transcript_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    json.dumps(final_state, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning(
                    "[convene-worker] failed to save transcript to %s: %s",
                    transcript_path, e,
                )

        # 4. Emit child tickets from the transcript.
        children = _parse_child_tickets(final_state)
        child_ids = _emit_child_tickets(
            conn, task_id, children, board=board
        ) if children else []

        # 5. Build a structured summary.
        votes = final_state.get("votes", {})
        vote_count = len(votes) if isinstance(votes, dict) else 0
        transcript_entries = len(final_state.get("transcript", []))
        status = final_state.get("status", "unknown")

        summary = (
            f"Convene {room_id} {status}. "
            f"{transcript_entries} transcript entries, "
            f"{vote_count} votes, {len(child_ids)} child tickets emitted. "
            f"Transcript: {transcript_path}"
        )

        return {
            "status": "completed",
            "summary": summary,
            "room_id": room_id,
            "child_tickets": child_ids,
            "transcript_path": transcript_path,
            "transcript_entries": transcript_entries,
            "vote_count": vote_count,
        }
    finally:
        conn.close()


def _complete_convene_task(
    task_id: str,
    result: dict,
    *,
    board: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Complete or block the convene task based on the worker result.

    Called by the __main__ entry point after run_convene_worker returns.
    """
    from hermes_cli import kanban_db as kb

    if db_path:
        conn = kb.connect(db_path=db_path, board=board)
    else:
        conn = kb.connect(board=board)
    try:
        if result.get("status") == "completed":
            kb.complete_task(
                conn,
                task_id,
                summary=result.get("summary", "convene completed"),
                metadata={
                    "room_id": result.get("room_id"),
                    "child_tickets": result.get("child_tickets", []),
                    "transcript_path": result.get("transcript_path"),
                    "transcript_entries": result.get("transcript_entries", 0),
                    "vote_count": result.get("vote_count", 0),
                },
                created_cards=result.get("child_tickets", []),
            )
        else:
            kb.block_task(
                conn,
                task_id,
                reason=result.get("error", "convene failed"),
                kind="transient",
            )
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m hermes_cli.convene_worker <task_id> [board]", file=sys.stderr)
        sys.exit(2)
    _task_id = sys.argv[1]
    _board = sys.argv[2] if len(sys.argv) > 2 else None
    _db_path = os.environ.get("HERMES_KANBAN_DB") or None
    _result = run_convene_worker(_task_id, board=_board, db_path=_db_path)
    _complete_convene_task(_task_id, _result, board=_board, db_path=_db_path)
    if _result.get("status") == "completed":
        print(_result.get("summary", "convene completed"))
        sys.exit(0)
    else:
        print(f"convene failed: {_result.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)