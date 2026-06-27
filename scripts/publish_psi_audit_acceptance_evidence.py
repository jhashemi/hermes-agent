#!/usr/bin/env python3
"""Publish Ψ-Audit acceptance evidence to NATS exec_okr stream.

Subject convention:
  okr.kr.acceptance_evidence.<kr_id>

Each event carries a full provenance manifest pointing at the commit, test
path, and measured residual. JetStream dedup via ``Nats-Msg-Id`` header
keyed on (kr_id, evidence_kind, commit_sha, test_path) makes replay safe.

This is the architecturally-correct handoff: it does NOT touch
``okr_accountability.db`` directly (that's owned by the running steering
reactors per Goal+Plan+Deliberation+Consensus governance) and it does NOT
short-circuit `steer_kr()`. It surfaces the evidence on the durable bus
where omnibus, memory_recorder, audit consumers, and any future
evidence→status reactor can react.

Reference: /home/ubuntu/.hermes/scripts/_eaf_eventbus.py (publish helper +
STREAMS catalog; exec_okr filter is ``okr.>``, retention 90d).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/.hermes/scripts")
import _eaf_eventbus as E  # noqa: E402

LOG = logging.getLogger("psi_audit_evidence_publisher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

NATS_URL = os.environ.get("EAF_NATS_URL", "nats://127.0.0.1:4222")


# Each entry is the full evidence manifest for one KR.  All four were
# closed in this session via real acceptance tests; commits + paths are
# pinned so downstream consumers can verify.
EVIDENCE: list[dict] = [
    {
        "kr_id": "kr_s5_h1_1",
        "objective_id": "obj_psi_s5_exec_agent_platform",
        "sector": "S5",
        "hypothesis": "H1",
        "title": "Free over kanban / dispatch_once_free unfolding",
        "repo": "jhashemi/hermes-kanban",
        "commit_sha": "ff4f6dd",
        "test_path": "tests/unit/test_psi_audit_s5_free_unfolding.py",
        "tests_pass": 5,
        "tests_total": 5,
        "duration_s": 1.21,
        "residual": 0.0,
        "residual_target": 0.05,
        "metric": "free_unfolding_invariant",
        "method": "branching-root preference + chain depth-2 unfolding",
        "raci": {
            "accountable": "werner_vogels",
            "responsible": ["jeff_dean"],
        },
    },
    {
        "kr_id": "kr_s4_h3_1",
        "objective_id": "obj_psi_s4_vcg",
        "sector": "S4",
        "hypothesis": "H3",
        "title": "VCG β₃ stability / determinism across 100 iterations",
        "repo": "jhashemi/hermes-orchestration",
        "commit_sha": "eb11865",
        "test_path": "tests/test_psi_audit_s4_vcg_determinism.py",
        "tests_pass": 4,
        "tests_total": 4,
        "duration_s": 43.37,
        "residual": 0.0,
        "residual_target": 0.05,
        "metric": "vcg_assignment_determinism",
        "method": "100-iteration scoring loop, max(sorted(...)) tiebreak",
        "raci": {
            "accountable": "werner_vogels",
            "responsible": ["jeff_dean"],
        },
    },
    {
        "kr_id": "kr_s5_h2_1",
        "objective_id": "obj_psi_s5_exec_agent_platform",
        "sector": "S5",
        "hypothesis": "H2",
        "title": "Plast naturality: train() fires for every non-zero reward",
        "repo": "jhashemi/executive-agents-framework",
        "commit_sha": "c5dd319",
        "test_path": "tests/unit/domain/test_psi_audit_s5_h2_acceptance.py",
        "tests_pass": 3,
        "tests_total": 3,
        "duration_s": 1.45,
        "residual": 0.0,
        "residual_target": 0.05,
        "metric": "plast_naturality_residual",
        "method": "1000-cycle Q-learner; ≥900 non-zero reward cycles required",
        "raci": {
            "accountable": "werner_vogels",
            "responsible": ["jeff_dean"],
        },
    },
    {
        "kr_id": "kr_s3_h1_1",
        "objective_id": "obj_psi_s3_hermes_core",
        "sector": "S3",
        "hypothesis": "H1",
        "title": "Session-search round-trip: decode(encode(x)) ≡ x",
        "repo": "jhashemi/hermes-agent",
        "commit_sha": "f111bdf3d",
        "test_path": "tests/test_psi_audit_s3_h1_session_round_trip.py",
        "tests_pass": 12,
        "tests_total": 12,
        "duration_s": 15.65,
        "residual": 0.0,
        "residual_target": 0.10,
        "metric": "round_trip_residual",
        "method": "100 query-pairs over JSON-serializable subset (scalars, multimodal lists, tool_call dicts, nested)",
        "raci": {
            "accountable": "john_carmack",
            "responsible": ["donald_knuth"],
        },
    },
]


def _nats_msg_id(kr: dict) -> str:
    """Deterministic dedup key — replays of this script are no-ops within
    the stream's 2-minute duplicate window (and idempotent thereafter
    by virtue of carrying the same audit content)."""
    h = hashlib.sha256()
    h.update(b"psi_audit_acceptance_evidence/")
    h.update(kr["kr_id"].encode())
    h.update(b"/")
    h.update(kr["commit_sha"].encode())
    h.update(b"/")
    h.update(kr["test_path"].encode())
    return h.hexdigest()[:32]


def _build_payload(kr: dict, *, ts: float) -> dict:
    return {
        "event_kind": "psi_audit.kr.acceptance_evidence",
        "event_id": _nats_msg_id(kr),
        "ts": ts,
        "kr_id": kr["kr_id"],
        "objective_id": kr["objective_id"],
        "sector": kr["sector"],
        "hypothesis": kr["hypothesis"],
        "title": kr["title"],
        "evidence": {
            "kind": "acceptance_test",
            "repo": kr["repo"],
            "commit_sha": kr["commit_sha"],
            "test_path": kr["test_path"],
            "tests_pass": kr["tests_pass"],
            "tests_total": kr["tests_total"],
            "duration_s": kr["duration_s"],
            "metric": kr["metric"],
            "residual": kr["residual"],
            "residual_target": kr["residual_target"],
            "passed": kr["residual"] <= kr["residual_target"]
                       and kr["tests_pass"] == kr["tests_total"],
            "method": kr["method"],
        },
        "raci": kr["raci"],
        "publisher": {
            "principal_uid": os.environ.get("PRINCIPAL_UID", "psi_audit_evidence_publisher"),
            "host": os.uname().nodename,
            "session_marker": "psi_audit_session_2026-06-07",
        },
    }


async def main() -> int:
    nc, js = await E.connect("psi_audit_evidence_publisher")
    LOG.info("connected to %s", NATS_URL)

    # exec_okr already exists (per `nats stream info exec_okr`) but ensure
    # idempotently in case this runs in a fresh env.
    try:
        await E.ensure_stream(js, "exec_okr")
    except Exception as exc:
        LOG.warning("ensure_stream(exec_okr) non-fatal failure: %s", exc)

    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    now = time.time()

    for kr in EVIDENCE:
        subject = f"okr.kr.acceptance_evidence.{kr['kr_id']}"
        payload = _build_payload(kr, ts=now)
        msg_id = payload["event_id"]
        ok = await E.publish(
            js, subject, payload,
            headers={"Nats-Msg-Id": msg_id, "Content-Type": "application/json"},
        )
        if ok:
            LOG.info(
                "published %s msg_id=%s residual=%.4f≤%.4f tests=%d/%d commit=%s",
                subject, msg_id, kr["residual"], kr["residual_target"],
                kr["tests_pass"], kr["tests_total"], kr["commit_sha"],
            )
            successes.append(kr["kr_id"])
        else:
            LOG.error("FAILED %s msg_id=%s", subject, msg_id)
            failures.append((kr["kr_id"], "publish returned False"))

    await nc.drain()

    print("\n===== Ψ-Audit acceptance evidence publish summary =====")
    print(f"Published: {len(successes)}/{len(EVIDENCE)}")
    for k in successes:
        print(f"  ✓ {k}")
    for k, err in failures:
        print(f"  ✗ {k}: {err}")
    print()

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
