"""CLI entrypoint for the L4→AGT compiler.

Usage:
    python -m tools.l4_agt_compiler.cli <path/to/system>/governance.l4 \\
        --out dist/ \\
        [--axis governance|runtime|integration|usage] \\
        [--service-url http://localhost:8080] \\
        [--ci-mode]

When ``--ci-mode`` is set, exit non-zero on any compile failure (no partial
output, no "best effort" — fail loudly so CI gates can block merges).
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

logger = logging.getLogger("l4_agt_compile")


_VALID_AXES = ("governance", "runtime", "integration", "usage", "deprecation")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="l4-agt-compile")
    p.add_argument("source", type=Path, help="Path to a single .l4 file (e.g. policies/<system>/governance.l4)")
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
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
