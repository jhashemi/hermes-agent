"""ACS manifest emitter — produces AGT-MANIFEST-1.0 JSON for the EAF/AGT policy_engine.

The AGT policy_engine consumes ACS (Agent Cognitive Substrate) manifests; the
canonical builder lives at ``agt.policies.bridge.governance_to_acs_manifest``.
We delegate to it whenever the AGT package is importable; otherwise we emit a
minimal stand-alone manifest that conforms to the ACS schema spec
(``agt/policy-engine/spec/agt/AGT-MANIFEST-1.0.md``).

The fallback path matters because the hermes-agent venv may not always have
``agt`` installed (it's a separate cluster project).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ACS_VERSION = "0.3.0-alpha-agt"
MANIFEST_SCHEMA = "AGT-MANIFEST-1.0"


def emit_acs_manifest(
    openapi_schema: Optional[dict],
    *,
    rego_path: Path,
    system: str,
    axis: str,
    rego_content: Optional[str] = None,
    enabled: bool = False,
    enforcement: str = "disabled",
) -> dict:
    """Build an ACS manifest dict for one (system, axis) policy.

    ``rego_path`` is the relative or absolute path to the .rego file that this
    manifest references. ``rego_content`` is optional but allows the manifest
    to embed a sha256 digest for tamper-evidence.

    Lifecycle fields
    ----------------
    ``enabled``     — when False the policy entry is **omitted** from the
                      ``policies`` dict (PolicyEngine never loads it) and its
                      id appears in ``omitted_disabled_policies`` instead.
    ``enforcement`` — ``"enforce"`` → ``default_decision: deny``
                      ``"monitor"`` or ``"disabled"`` → ``default_decision: allow``
    """
    if not system:
        raise ValueError("system is required")
    if not axis:
        raise ValueError("axis is required")

    policy_id = f"{system}.{axis}"
    rego_digest = None
    if rego_content is not None:
        rego_digest = "sha256:" + hashlib.sha256(rego_content.encode("utf-8")).hexdigest()
    elif rego_path.is_file():
        rego_digest = "sha256:" + hashlib.sha256(rego_path.read_bytes()).hexdigest()

    fn_count = 0
    if isinstance(openapi_schema, dict):
        paths = openapi_schema.get("paths") or {}
        fn_count = sum(
            1 for p in paths.keys()
            if isinstance(p, str) and p.rstrip("/").endswith("/evaluation")
        )

    manifest: dict = {
        "agent_control_specification_version": ACS_VERSION,
        "manifest_schema": MANIFEST_SCHEMA,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "source": {
            "system": system,
            "axis": axis,
            "compiler": "tools/l4_agt_compiler@0.1.0-dev",
            "exported_function_count": fn_count,
        },
    }

    if not enabled:
        # Omit disabled policies from the manifest; note them for visibility
        manifest["policies"] = {}
        manifest["omitted_disabled_policies"] = [policy_id]
    else:
        default_decision = "deny" if enforcement == "enforce" else "allow"
        manifest["policies"] = {
            policy_id: {
                "type": "rego",
                "path": str(rego_path),
                "digest": rego_digest,
                "package": f"governance.{system.replace('-', '_')}.{axis.replace('-', '_')}",
                "enabled": enabled,
                "enforcement": enforcement,
                "default_decision": default_decision,
            }
        }
        manifest["omitted_disabled_policies"] = []

    return manifest
