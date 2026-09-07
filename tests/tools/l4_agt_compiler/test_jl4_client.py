"""TDD tests for the L4→AGT jl4-service client.

These tests inject HTTP stubs so they don't require a live jl4-service.
Integration smoke tests against the real service should live in a
separately-marked file gated by JL4_SERVICE_URL env (TBD).
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from tools.l4_agt_compiler.jl4_client import (
    CompileResult,
    compile_bundle,
    _build_bundle_zip,
)


@pytest.fixture
def tmp_l4(tmp_path) -> Path:
    src = tmp_path / "demo.l4"
    src.write_text(
        "-- system: demo\n"
        "-- axis: governance\n"
        "-- tier: T3\n"
        "DECLARE Foo IS A Type\n"
    )
    return src


def test_build_bundle_zip_is_valid(tmp_l4):
    blob = _build_bundle_zip([tmp_l4])
    assert blob.startswith(b"PK")  # ZIP local-file-header magic
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.namelist() == ["demo.l4"]
        assert zf.read("demo.l4").startswith(b"-- system: demo")


def test_build_bundle_zip_rejects_empty():
    with pytest.raises(ValueError):
        _build_bundle_zip([])


def test_build_bundle_zip_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        _build_bundle_zip([tmp_path / "does-not-exist.l4"])


def test_compile_bundle_happy_path(tmp_l4):
    """POST /deployments → applied → /openapi.json — all happy."""
    captured = {}

    def fake_post(url, *, body, content_type, headers, timeout):
        captured["post_url"] = url
        captured["body_size"] = len(body)
        captured["content_type"] = content_type
        return {"id": "deploy-abc", "updateId": "job-1", "status": "compiling"}

    schema_payload = {
        "paths": {
            "/deployments/deploy-abc/functions/permit/evaluation": {
                "post": {"summary": "permit"}
            }
        }
    }

    poll_seq = iter([
        {"status": "compiling", "errors": []},
        {"status": "applied", "errors": []},
        schema_payload,
    ])

    def fake_get(url, *, timeout):
        return next(poll_seq)

    sleeps: list[float] = []
    result = compile_bundle(
        [tmp_l4],
        deployment_id="deploy-abc",
        base_url="http://fake-jl4:8080",
        poll_deadline_seconds=5,
        poll_interval_seconds=0.01,
        _http_post=fake_post,
        _http_get=fake_get,
        _sleep=sleeps.append,
    )

    assert result.is_success, result
    assert result.status == "applied"
    assert result.deployment_id == "deploy-abc"
    assert result.openapi_schema == schema_payload
    assert captured["content_type"] == "application/zip"
    assert captured["body_size"] > 0


def test_compile_bundle_reports_compile_failed(tmp_l4):
    """jl4-service polls back compile_failed → result captures errors."""

    def fake_post(url, *, body, content_type, headers, timeout):
        return {"id": "deploy-bad", "updateId": "job-1", "status": "compiling"}

    poll_seq = iter([
        {"status": "compiling", "errors": []},
        {"status": "compile_failed", "errors": ["unbound reference 'Bar'"]},
    ])

    def fake_get(url, *, timeout):
        return next(poll_seq)

    result = compile_bundle(
        [tmp_l4],
        deployment_id="deploy-bad",
        poll_deadline_seconds=5,
        poll_interval_seconds=0.01,
        _http_post=fake_post,
        _http_get=fake_get,
        _sleep=lambda _: None,
    )

    assert not result.is_success
    assert result.status == "compile_failed"
    assert "unbound reference 'Bar'" in result.errors


def test_compile_bundle_handles_post_connection_error(tmp_l4):
    """Service unreachable on initial POST → service_error result, not exception."""
    import urllib.error

    def fake_post(url, *, body, content_type, headers, timeout):
        raise urllib.error.URLError("connection refused")

    result = compile_bundle(
        [tmp_l4],
        deployment_id="deploy-x",
        _http_post=fake_post,
        _http_get=lambda *a, **k: pytest.fail("should not poll after POST failure"),
        _sleep=lambda _: None,
    )

    assert result.status == "service_error"
    assert "connection refused" in result.errors[0]


def test_compile_bundle_times_out_on_stuck_poll(tmp_l4):
    """If poll never leaves 'compiling', result is timed_out (not infinite loop)."""

    def fake_post(url, *, body, content_type, headers, timeout):
        return {"id": "deploy-stuck", "updateId": "job-1", "status": "compiling"}

    def fake_get(url, *, timeout):
        return {"status": "compiling", "errors": []}

    # Use a tiny deadline; advance "time" by sleeping more than that
    import time

    real_monotonic = time.monotonic
    base = real_monotonic()
    fake_now = [base]

    def fake_sleep(s):
        fake_now[0] += 1.0  # each tick advances 1s

    monkey_monotonic = lambda: fake_now[0]
    import tools.l4_agt_compiler.jl4_client as mod
    orig = mod.time.monotonic
    mod.time.monotonic = monkey_monotonic  # type: ignore[assignment]
    try:
        result = compile_bundle(
            [tmp_l4],
            deployment_id="deploy-stuck",
            poll_deadline_seconds=2,
            poll_interval_seconds=0.01,
            _http_post=fake_post,
            _http_get=fake_get,
            _sleep=fake_sleep,
        )
    finally:
        mod.time.monotonic = orig  # type: ignore[assignment]

    assert result.status == "timed_out"
