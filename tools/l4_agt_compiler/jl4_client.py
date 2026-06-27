"""jl4-service client.

Talks to a running jl4-service over HTTP. The service is the legalese/l4-ide
canonical typechecker + evaluator — we do NOT write our own parser.

Service endpoints (per jl4-service src/Schema.hs):
    POST /deployments                              -- upload .l4 zip bundle
    GET  /deployments/{id}                         -- check status
    GET  /deployments/{id}/updates/{job_id}        -- poll compile job
    GET  /deployments/{id}/openapi.json            -- per-deployment schema
    GET  /openapi.json                             -- org-wide schema
    GET  /health

The service does not expose raw CoreL4 AST over REST — the closest thing is
the per-deployment OpenAPI spec, which describes every @export'd function's
signature. That's what we walk in the Rego/ACS emitters.

For full AST fidelity we fall back to a subprocess invocation of
    cabal run l4 -- run --json <file>
guarded behind the `--ast-fidelity=full` CLI flag (not implemented in v0.1).
"""
from __future__ import annotations

import io
import logging
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import urllib.error
import urllib.request
import json as _json

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_POLL_DEADLINE_SECONDS = 30


@dataclass
class CompileResult:
    """Result of compiling an L4 bundle through jl4-service.

    ``status`` mirrors jl4-service's deployment-update statuses:
      * "applied"        — compile + typecheck succeeded; openapi_schema valid
      * "compile_failed" — compile or typecheck failure; errors populated
      * "timed_out"      — poll deadline exceeded (treated as failure for CI)
      * "service_error"  — jl4-service returned 5xx or unreachable
    """

    status: str
    deployment_id: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    openapi_schema: Optional[dict] = None

    @property
    def is_success(self) -> bool:
        return self.status == "applied"


def _build_bundle_zip(l4_files: list[Path]) -> bytes:
    """Pack a list of .l4 files into a flat zip bundle for /deployments upload."""
    if not l4_files:
        raise ValueError("at least one .l4 file required")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in l4_files:
            if not path.is_file():
                raise FileNotFoundError(f"L4 source not found: {path}")
            zf.write(path, arcname=path.name)
    return buf.getvalue()


def compile_bundle(
    l4_files: list[Path],
    *,
    deployment_id: str,
    base_url: str = DEFAULT_BASE_URL,
    poll_deadline_seconds: int = DEFAULT_POLL_DEADLINE_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    request_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    _http_post=None,
    _http_get=None,
    _sleep=None,
) -> CompileResult:
    """Upload a bundle, poll until applied or failed, fetch the schema.

    The ``_http_post``/``_http_get``/``_sleep`` parameters exist so unit tests
    can inject stubs without spinning up a real jl4-service. When omitted, the
    real urllib transport is used.
    """
    http_post = _http_post or _default_http_post
    http_get = _http_get or _default_http_get
    sleep = _sleep or time.sleep

    bundle_bytes = _build_bundle_zip(l4_files)

    try:
        resp = http_post(
            f"{base_url}/deployments",
            body=bundle_bytes,
            content_type="application/zip",
            headers={"X-Deployment-Id": deployment_id},
            timeout=request_timeout_seconds,
        )
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        return CompileResult(
            status="service_error",
            deployment_id=deployment_id,
            errors=[f"jl4-service POST /deployments failed: {exc}"],
        )

    deploy_meta = resp if isinstance(resp, dict) else _safe_json(resp)
    if not deploy_meta:
        return CompileResult(
            status="service_error",
            deployment_id=deployment_id,
            errors=["jl4-service returned non-JSON response to /deployments"],
        )
    actual_id = deploy_meta.get("id") or deployment_id
    job_id = deploy_meta.get("updateId") or deploy_meta.get("jobId")
    initial_status = deploy_meta.get("status", "compiling")

    deadline = time.monotonic() + poll_deadline_seconds
    status = initial_status
    errors: list[str] = []
    while status in ("compiling", "queued", "pending"):
        if time.monotonic() > deadline:
            return CompileResult(
                status="timed_out",
                deployment_id=actual_id,
                errors=[f"compile poll exceeded {poll_deadline_seconds}s deadline"],
            )
        sleep(poll_interval_seconds)
        if not job_id:
            poll_url = f"{base_url}/deployments/{actual_id}"
        else:
            poll_url = f"{base_url}/deployments/{actual_id}/updates/{job_id}"
        try:
            poll = http_get(poll_url, timeout=request_timeout_seconds)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            return CompileResult(
                status="service_error",
                deployment_id=actual_id,
                errors=[f"jl4-service poll failed: {exc}"],
            )
        if isinstance(poll, dict):
            status = poll.get("status", status)
            errors = poll.get("errors", []) or []

    if status != "applied":
        return CompileResult(
            status=status if status != "compiling" else "compile_failed",
            deployment_id=actual_id,
            errors=errors,
        )

    try:
        schema = http_get(
            f"{base_url}/deployments/{actual_id}/openapi.json",
            timeout=request_timeout_seconds,
        )
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        return CompileResult(
            status="service_error",
            deployment_id=actual_id,
            errors=[f"jl4-service openapi fetch failed: {exc}"],
        )

    return CompileResult(
        status="applied",
        deployment_id=actual_id,
        errors=[],
        openapi_schema=schema if isinstance(schema, dict) else None,
    )


def health_check(base_url: str = DEFAULT_BASE_URL, *, timeout: int = 5) -> bool:
    """GET /health → 200 OK?"""
    try:
        resp = _default_http_get(f"{base_url}/health", timeout=timeout)
        return resp is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# urllib helpers (split out so tests can stub them)
# ---------------------------------------------------------------------------


def _default_http_post(url: str, *, body: bytes, content_type: str, headers: dict, timeout: int):
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return _safe_json(raw)


def _default_http_get(url: str, *, timeout: int):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return _safe_json(raw)


def _safe_json(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            return _json.loads(raw.decode("utf-8"))
        except Exception:
            return None
    if isinstance(raw, str):
        try:
            return _json.loads(raw)
        except Exception:
            return None
    return None
