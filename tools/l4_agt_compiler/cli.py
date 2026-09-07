"""CLI entrypoint for the L4→AGT compiler.

Subcommands
-----------
compile (default positional)
    python -m tools.l4_agt_compiler.cli <path/to/system>/governance.l4 \\
        --out dist/ \\
        [--axis governance|runtime|integration|usage] \\
        [--service-url http://localhost:8080] \\
        [--ci-mode]

policy
    python -m tools.l4_agt_compiler.cli policy enable  <system>.<axis> [--changed-by ID]
    python -m tools.l4_agt_compiler.cli policy disable <system>.<axis> --reason "..." [--changed-by ID]
    python -m tools.l4_agt_compiler.cli policy monitor <system>.<axis> [--changed-by ID]
    python -m tools.l4_agt_compiler.cli policy enforce <system>.<axis> [--changed-by ID]
    python -m tools.l4_agt_compiler.cli policy status  [<system>.<axis>]

When ``--ci-mode`` is set for compile, exit non-zero on any compile failure (no
partial output, no "best effort" — fail loudly so CI gates can block merges).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .jl4_client import compile_bundle, health_check, DEFAULT_BASE_URL
from .rego_emitter import emit_rego
from .acs_emitter import emit_acs_manifest
from .policy_state import (
    DEFAULT_STATE_PATH,
    load_state,
    set_policy_state,
    get_policy_state,
)

logger = logging.getLogger("l4_agt_compile")


_VALID_AXES = ("governance", "runtime", "integration", "usage", "deprecation")


# ── argument parsing ──────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="l4-agt-compile",
        description="L4→AGT compiler and policy lifecycle manager",
    )
    root.add_argument("--quiet", action="store_true", help="Suppress info logging")

    subparsers = root.add_subparsers(dest="subcommand")

    # ── compile subcommand (also the legacy implicit path) ───────────────────
    compile_p = subparsers.add_parser("compile", help="Compile a .l4 file to Rego + ACS manifest")
    _add_compile_args(compile_p)

    # ── policy subcommand ────────────────────────────────────────────────────
    policy_p = subparsers.add_parser("policy", help="Manage policy enable/disable lifecycle")
    policy_sub = policy_p.add_subparsers(dest="policy_action")

    for action in ("enable", "monitor", "enforce"):
        sp = policy_sub.add_parser(action, help=f"Set policy to {action}")
        sp.add_argument("policy_id", help="<system>.<axis> identifier")
        sp.add_argument("--changed-by", default="operator", help="Actor performing the change")
        sp.add_argument(
            "--state-path",
            type=Path,
            default=DEFAULT_STATE_PATH,
            help="Path to .state.json override",
        )

    disable_p = policy_sub.add_parser("disable", help="Disable a policy (break-glass)")
    disable_p.add_argument("policy_id", help="<system>.<axis> identifier")
    disable_p.add_argument("--reason", required=True, help="Reason for disabling (required)")
    disable_p.add_argument("--changed-by", default="operator", help="Actor performing the change")
    disable_p.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="Path to .state.json override",
    )

    status_p = policy_sub.add_parser("status", help="Show policy state")
    status_p.add_argument(
        "policy_id",
        nargs="?",
        default=None,
        help="<system>.<axis> — omit to list all policies",
    )
    status_p.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="Path to .state.json override",
    )

    return root


def _add_compile_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "source",
        type=Path,
        help="Path to a single .l4 file (e.g. policies/<system>/governance.l4)",
    )
    p.add_argument("--out", type=Path, default=Path("dist"), help="Output directory")
    p.add_argument(
        "--axis",
        choices=_VALID_AXES,
        default=None,
        help="Override the axis (otherwise inferred from filename stem)",
    )
    p.add_argument("--service-url", default=DEFAULT_BASE_URL, help="jl4-service base URL")
    p.add_argument(
        "--ci-mode",
        action="store_true",
        help="Exit non-zero on any compile failure",
    )
    # --quiet is also accepted here for legacy callers that put it after the source
    p.add_argument("--quiet", action="store_true", default=False, help="Suppress info logging")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = _build_parser()

    # Legacy compatibility: if the first non-flag arg looks like a file path
    # (not "compile" or "policy"), inject "compile" so existing callers still work.
    if argv is None:
        argv = sys.argv[1:]

    # Detect legacy invocation: first meaningful token is a file path, not a subcommand
    effective = [a for a in argv if not a.startswith("-")]
    if effective and effective[0] not in ("compile", "policy"):
        argv = ["compile"] + list(argv)

    return parser.parse_args(argv)


# ── compile handler ───────────────────────────────────────────────────────────


def _infer_system_axis(source: Path, axis_override: str | None) -> tuple[str, str]:
    """policies/<system>/<axis>.l4 → (system, axis). Falls back to parent dir + stem."""
    stem = source.stem
    axis = axis_override or stem
    if axis not in _VALID_AXES:
        raise ValueError(
            f"axis '{axis}' not in {_VALID_AXES}; pass --axis explicitly"
        )
    system = source.parent.name
    if not system:
        raise ValueError(f"could not infer system name from {source}")
    return system, axis


def _run_compile(args: argparse.Namespace) -> int:
    if not args.source.is_file():
        logger.error("source not found: %s", args.source)
        return 2

    system, axis = _infer_system_axis(args.source, args.axis)
    args.out.mkdir(parents=True, exist_ok=True)

    logger.info("compiling %s.%s via %s", system, axis, args.service_url)

    if not health_check(args.service_url):
        logger.warning(
            "jl4-service unreachable at %s — will still attempt compile (errors will surface)",
            args.service_url,
        )

    deployment_id = f"hermes-l4-{system}-{axis}"
    result = compile_bundle(
        [args.source],
        deployment_id=deployment_id,
        base_url=args.service_url,
    )

    if not result.is_success:
        logger.error(
            "L4 compile failed: status=%s errors=%s",
            result.status,
            result.errors,
        )
        if args.ci_mode:
            return 1
        # Still emit a "compile-failed" stub so downstream tooling can detect it
        stub_path = args.out / f"{system}.{axis}.compile_failed.json"
        stub_path.write_text(json.dumps({
            "status": result.status,
            "errors": result.errors,
            "system": system,
            "axis": axis,
        }, indent=2))
        return 1

    rego = emit_rego(result.openapi_schema, system=system, axis=axis)
    rego_path = args.out / f"{system}.{axis}.rego"
    rego_path.write_text(rego)

    manifest = emit_acs_manifest(
        result.openapi_schema,
        rego_path=rego_path,
        system=system,
        axis=axis,
        rego_content=rego,
    )
    manifest_path = args.out / f"{system}.{axis}.acs.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    logger.info(
        "wrote %s and %s",
        rego_path,
        manifest_path,
    )
    return 0


# ── policy handlers ───────────────────────────────────────────────────────────


def _run_policy(args: argparse.Namespace) -> int:
    action = args.policy_action
    if action is None:
        print("Usage: l4-agt-compile policy <enable|disable|monitor|enforce|status>", file=sys.stderr)
        return 2

    state_path = getattr(args, "state_path", DEFAULT_STATE_PATH)

    if action == "status":
        return _policy_status(args, state_path)

    if action == "enable":
        entry = set_policy_state(
            args.policy_id,
            enabled=True,
            enforcement="monitor",  # enable → monitor mode by default
            changed_by=args.changed_by,
            state_path=state_path,
        )
        print(f"Policy {args.policy_id!r} enabled (enforcement=monitor).")
        print(f"  changed_by: {entry['changed_by']}")
        print(f"  at: {entry['last_changed_at']}")
        return 0

    if action == "disable":
        entry = set_policy_state(
            args.policy_id,
            enabled=False,
            enforcement="disabled",
            changed_by=args.changed_by,
            reason=args.reason,
            state_path=state_path,
        )
        print(f"Policy {args.policy_id!r} DISABLED.")
        print(f"  reason: {entry['reason']}")
        print(f"  auto_revert_at: {entry.get('auto_revert_at', 'N/A')}")
        print(f"  changed_by: {entry['changed_by']}")
        return 0

    if action == "monitor":
        entry = set_policy_state(
            args.policy_id,
            enabled=True,
            enforcement="monitor",
            changed_by=args.changed_by,
            state_path=state_path,
        )
        print(f"Policy {args.policy_id!r} set to monitor mode.")
        print(f"  changed_by: {entry['changed_by']}")
        print(f"  at: {entry['last_changed_at']}")
        return 0

    if action == "enforce":
        entry = set_policy_state(
            args.policy_id,
            enabled=True,
            enforcement="enforce",
            changed_by=args.changed_by,
            state_path=state_path,
        )
        print(f"Policy {args.policy_id!r} set to ENFORCE mode.")
        print(f"  changed_by: {entry['changed_by']}")
        print(f"  at: {entry['last_changed_at']}")
        return 0

    print(f"Unknown policy action: {action!r}", file=sys.stderr)
    return 2


def _policy_status(args: argparse.Namespace, state_path: Path) -> int:
    state = load_state(state_path)
    policies = state.get("policies", {})

    if args.policy_id is not None:
        # Single policy
        pid = args.policy_id
        if pid in policies:
            entry = policies[pid]
            print(f"Policy: {pid}")
            print(f"  enabled:     {entry.get('enabled')}")
            print(f"  enforcement: {entry.get('enforcement')}")
            print(f"  changed_by:  {entry.get('changed_by', 'N/A')}")
            print(f"  changed_at:  {entry.get('last_changed_at', 'N/A')}")
            if entry.get("reason"):
                print(f"  reason:      {entry['reason']}")
            if entry.get("auto_revert_at"):
                print(f"  auto_revert: {entry['auto_revert_at']}")
        else:
            print(f"Policy {pid!r} has no runtime state (header defaults apply).")
        return 0

    # List all
    if not policies:
        print("No policies in state file.")
        return 0

    print(f"{'Policy ID':<35}  {'enabled':<8}  {'enforcement':<12}  {'changed_by'}")
    print("-" * 75)
    for pid, entry in sorted(policies.items()):
        print(
            f"{pid:<35}  {str(entry.get('enabled', '?')):<8}  "
            f"{entry.get('enforcement', '?'):<12}  {entry.get('changed_by', 'N/A')}"
        )
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # --quiet may be on the root namespace or on the compile sub-namespace
    quiet = getattr(args, "quiet", False)
    logging.basicConfig(
        level=logging.WARNING if quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.subcommand == "policy":
        return _run_policy(args)

    # "compile" subcommand (or legacy positional)
    return _run_compile(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
