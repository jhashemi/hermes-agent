# L4 → AGT Compiler

Compiles L4 (jl4) governance policies → **Rego** (for OPA) + **ACS manifest** (for EAF/AGT policy_engine).

**Status: v0.1.0-dev — scaffolding + emitters + CLI; jl4-service Docker not yet vendored.**

## Pipeline

```
.l4 source
    │
    ▼  (jl4_client.compile_bundle)
jl4-service Docker (legalese/l4-ide canonical)
    │ POST /deployments → poll /updates/{job_id} → /openapi.json
    ▼
OpenAPI schema (per-deployment) — closest reflection of typechecked AST surface
    │
    ├──► (rego_emitter.emit_rego)  → dist/<system>.<axis>.rego
    │
    └──► (acs_emitter.emit_acs_manifest)  → dist/<system>.<axis>.acs.json
                ↑
                └─ delegates to agt.policies.bridge.governance_to_acs_manifest
                   if AGT pkg is importable; falls back to in-tree builder otherwise
```

## CLI

```
python -m tools.l4_agt_compiler.cli \
    /home/ubuntu/nebula-cognitive-governance/policies/hrv-pacemaker/governance.l4 \
    --out /tmp/dist \
    --service-url http://localhost:8080 \
    --ci-mode
```

Output:
- `/tmp/dist/hrv-pacemaker.governance.rego`
- `/tmp/dist/hrv-pacemaker.governance.acs.json`

## Why we don't write our own parser

The L4 DSL descends from the legalese/l4-ide jl4 toolchain. Per ADR-011 §"Policy Catalog & jl4-service":
> CI runs `jl4-service /typecheck`; on tag, CI runs `tools/l4_agt_compiler/` producing Rego + ACS manifests.

Writing our own parser/typechecker would be a 6-month project that introduces drift from the canonical toolchain. The compiler treats jl4-service as a black box and consumes its OpenAPI emission.

## Known limitations (v0.1.0-dev)

1. **No raw AST.** jl4-service does not expose CoreL4 AST over REST. We use the per-deployment OpenAPI schema as the function-spec surface. For full AST fidelity, plan: subprocess `cabal run l4 -- run --json <file>` behind a `--ast-fidelity=full` flag.
2. **Allow-only Rego emission.** Obligation/deny rule emission (e.g. `MUST_*` predicates → Rego `deny[msg]`) is tracked separately. v0.1 emits one boolean `allow if { input.<fn> == true }` per `@export`'d L4 function.
3. **jl4-service vendoring.** Docker compose at `tools/l4_agt_compiler/docker/docker-compose.yml` (TBD); user must currently run jl4-service themselves on `localhost:8080`. Native build fallback via `JL4_NATIVE_BUILD=1` is planned.
4. **No live tests against real jl4-service.** Unit tests use injected HTTP stubs (see `tests/tools/l4_agt_compiler/`). Integration smoke against the real service is gated behind `JL4_SERVICE_URL` env var (skipped if unset).

## OKR alignment

- `okr-l4-agt-bridge` KR1: this compiler.
- `okr-l4-agt-bridge` KR5: jl4 syntax compatibility audit (separate work).
- `okr-l4-catalog-completion` KR1: jl4 KB lift (separate work).

## TDD coverage

| Test file | What it locks down |
|---|---|
| `tests/tools/l4_agt_compiler/test_jl4_client.py` | Bundle zip, POST/poll/openapi flow with stubbed HTTP |
| `tests/tools/l4_agt_compiler/test_rego_emitter.py` | Header structure, package naming, allow-rule emission |
| `tests/tools/l4_agt_compiler/test_acs_emitter.py` | Manifest schema, sha256 digest, ACS_VERSION |
| `tests/tools/l4_agt_compiler/test_cli.py` | Round-trip, ci-mode exit codes, axis inference |
