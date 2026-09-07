"""Regression tests for GLM context-length resolution (2026-08 Z.ai refresh).

GLM-5.3 (released 2026-08-18) has a 1M-token context window per
https://docs.z.ai/guides/llm/glm-5.3 — the same 1 Mi (1,048,576) window
GLM-5.2 uses.  Before the hardcoded catalog entry existed, ``glm-5.3``
fell through to the generic ``"glm": 202_752`` fallback and under-reported
the window by ~5x.
"""

from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS


def _resolve_via_substring_catalog(model_lower: str):
    """Mirror the longest-key-first substring resolution used by the
    context-length resolver (step 8 in ``get_context_length``)."""
    hits = sorted(
        ((k, v) for k, v in DEFAULT_CONTEXT_LENGTHS.items() if k in model_lower),
        key=lambda x: len(x[0]),
        reverse=True,
    )
    return hits[0][1] if hits else None


def test_glm_5_3_has_explicit_catalog_entry():
    assert DEFAULT_CONTEXT_LENGTHS.get("glm-5.3") == 1_048_576


def test_glm_5_3_resolves_to_1m_not_202k_fallback():
    # The regression this guards: without the explicit entry, the bare
    # "glm" catch-all (202_752) would win for "glm-5.3".
    assert _resolve_via_substring_catalog("glm-5.3") == 1_048_576


def test_glm_5_2_still_resolves_to_1m():
    assert _resolve_via_substring_catalog("glm-5.2") == 1_048_576


def test_older_glm_variants_still_hit_202k_fallback():
    for variant in ("glm-5.1", "glm-5", "glm-5-turbo"):
        assert _resolve_via_substring_catalog(variant) == 202_752, variant


def test_models_dev_cache_lookup_for_zai_glm_5_3():
    """Disk-cache path: the zai provider entry must carry glm-5.3 with a
    1M context window (mirrors models.dev schema: limit.context)."""
    import json
    from agent.models_dev import _get_cache_path, lookup_models_dev_context

    # Disk cache is host-local; skip silently when absent so this test is
    # portable across CI machines that never wrote a cache.
    cache_path = _get_cache_path()
    if not cache_path.exists():
        return

    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)
    entry = data.get("zai", {}).get("models", {}).get("glm-5.3")
    assert entry is not None, "glm-5.3 missing from models.dev disk cache (zai)"
    assert entry.get("limit", {}).get("context") == 1_000_000
    assert entry.get("reasoning") is True

    # In-memory/disk merge path used by the resolver.
    assert lookup_models_dev_context("zai", "glm-5.3") == 1_000_000
