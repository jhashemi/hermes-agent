"""Guard tests for the voice-agents agents_registry.yaml.

Root cause class (found 2026-09-03, wave 20260903w): a bad merge committed a
4-space-indented line inside the 0-indent `metadata:` block, making the file
unparseable YAML — while the committing wave's test suite ran green because it
validated code, never the registry data file. Same wave, `demis_hassabis` was
marked voice_ready=true with no voice_uuid, and the commit message recorded a
clone UUID that actually belongs to jony_ive.

These tests parse the REAL registry file so a corrupt push can never pass CI.
"""

from pathlib import Path

import pytest
import yaml

REGISTRY = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "voice-agents" / "agents" / "agents_registry.yaml"
)

# Canonical clone identity, verified against Resemble /v2/voices on 2026-09-03:
#   63d20b1d = "VoiceTwin Orion" (demis_hassabis), status=finished.
# If the owner legitimately re-clones Orion, update this constant WITH a fresh
# API verification note — never by editing the registry alone.
CANONICAL_VOICE_UUIDS = {
    "demis_hassabis": "63d20b1d",
    "jony_ive": "9d513c17",
    "taylor": "4e972f71",
    # VoiceTwin Tigani (live /synthesize round-trip verified 2026-09-03, t_4f3c1a06)
    "jordan_tigani": "0f2f9a7e",
    # "Marc Byers" (Resemble /v2/voices API-verified 2026-09-03)
    "marc": "ee15cd5a",
}


@pytest.fixture(scope="module")
def registry():
    assert REGISTRY.exists(), f"registry file missing: {REGISTRY}"
    with open(REGISTRY) as f:
        data = yaml.safe_load(f)  # must not raise
    assert isinstance(data, dict), "registry root must be a mapping"
    return data


def test_registry_yaml_parses(registry):
    agents = registry.get("agents")
    assert isinstance(agents, dict) and agents, "agents: block missing or empty"


def test_registry_metadata_counters_consistent(registry):
    agents = registry["agents"]
    meta = registry.get("metadata", {})
    assert meta.get("total_agents") == len(agents), (
        f"metadata.total_agents={meta.get('total_agents')} "
        f"but file defines {len(agents)} agents"
    )
    n_interviewed = sum(
        1 for a in agents.values() if a.get("interview_complete") is True
    )
    assert meta.get("interview_complete") == n_interviewed, (
        "metadata.interview_complete drifted from actual count"
    )
    n_voice = sum(1 for a in agents.values() if a.get("voice_ready") is True)
    assert meta.get("voice_enabled") == n_voice, (
        "metadata.voice_enabled drifted from actual voice_ready count"
    )


def test_every_voice_ready_agent_has_voice_uuid(registry):
    for name, a in registry["agents"].items():
        if a.get("voice_ready"):
            uuid = a.get("voice_uuid")
            assert uuid, f"{name}: voice_ready=true but voice_uuid missing"
            assert not str(uuid).startswith("pending_clone_"), (
                f"{name}: voice_ready=true but uuid is a placeholder"
            )


def test_voice_uuids_globally_unique(registry):
    seen = {}
    for name, a in registry["agents"].items():
        u = a.get("voice_uuid")
        if u:
            assert u not in seen, (
                f"voice_uuid collision: {name} and {seen[u]} both claim {u}"
            )
            seen[u] = name


def test_canonical_clone_identities(registry):
    for name, expected in CANONICAL_VOICE_UUIDS.items():
        a = registry["agents"].get(name)
        assert a is not None, f"{name} missing from registry"
        assert a.get("voice_ready") is True, f"{name} must be voice_ready"
        assert a.get("voice_uuid") == expected, (
            f"{name}: voice_uuid={a.get('voice_uuid')!r} != canonical "
            f"{expected!r} (API-verified 2026-09-03)"
        )
