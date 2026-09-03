"""Guard tests for the voice-agents agents_registry.yaml.

Root cause class (found 2026-09-03, wave 20260903w): a bad merge committed a
4-space-indented line inside the 0-indent `metadata:` block, making the file
unparseable YAML — while the committing wave's test suite ran green because it
validated code, never the registry data file. Same wave, `demis_hassabis` was
marked voice_ready=true with no voice_uuid, and the commit message recorded a
clone UUID that actually belongs to jony_ive.

These tests parse the REAL registry file so a corrupt push can never pass CI.

Wave 20260903x addendum — deploy-path drift (found 2026-09-03): wave-w repaired
and guarded the REPO copy, but the INSTALLED production copy under
~/.hermes/plugins/voice-agents/agents/ was still the corrupt pre-repair file
(mtime 20:14 vs repair commit 21:27). A suite that validates only the source
tree is green while the live fleet parses garbage. The suite therefore runs
every invariant against BOTH paths, plus an explicit sync test.
"""

from pathlib import Path

import pytest
import yaml

REPO = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "voice-agents" / "agents" / "agents_registry.yaml"
)
# Production path actually read by the deployed fleet (spawn_agent_service,
# bridge handlers). If this drifts from REPO, the repo guard is guarding
# nothing — see test_installed_in_sync_with_repo.
INSTALLED = (
    Path.home() / ".hermes" / "plugins" / "voice-agents" / "agents"
    / "agents_registry.yaml"
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


def _load(path):
    assert path.exists(), f"registry file missing: {path}"
    with open(path) as f:
        data = yaml.safe_load(f)  # must not raise
    assert isinstance(data, dict), "registry root must be a mapping"
    return data


@pytest.fixture(scope="module", params=["repo", "installed"])
def registry(request):
    """Run every invariant against BOTH the repo copy and the deployed copy."""
    return _load(REPO if request.param == "repo" else INSTALLED)


def test_installed_copy_exists():
    # A missing deployed artifact is itself a deploy failure.
    assert INSTALLED.exists(), f"installed registry missing: {INSTALLED}"


def test_installed_in_sync_with_repo():
    """The deployed fleet must parse byte-identical content to the repo copy.

    Guards against wave-w-class deploy drift: repo repaired + guarded while the
    production path keeps serving the corrupt pre-repair file.
    """
    assert INSTALLED.exists(), f"installed registry missing: {INSTALLED}"
    if REPO.resolve() == INSTALLED.resolve():
        pytest.skip("plugin deployed via symlink; single artifact")
    repo_bytes = REPO.read_bytes()
    inst_bytes = INSTALLED.read_bytes()
    assert inst_bytes == repo_bytes, (
        "installed registry drifted from repo copy — deploy the repaired "
        "registry (repo plugins/voice-agents/agents/agents_registry.yaml) to "
        "~/.hermes/plugins/voice-agents/agents/ before trusting a green suite"
    )


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
