"""L4 → AGT compiler.

Compiles L4 (jl4) governance policies to AGT-consumable artifacts:
  * Rego (.rego) — for Open Policy Agent / OPA evaluation
  * ACS manifest (AGT-MANIFEST-1.0 JSON) — for the EAF/AGT policy_engine

Pipeline:
    .l4 source
       │
       ▼  (Step 1) zip + POST /deployments
    jl4-service Docker (ghcr-built; native fallback)
       │
       ▼  (Step 2) typecheck + emit OpenAPI schema
    Function spec
       │
       ▼  (Step 3) walk schema, emit Rego module
       ▼  (Step 4) emit ACS manifest (delegates to agt.policies.bridge)
       │
       ▼  dist/<system>.<axis>.rego + dist/<system>.<axis>.acs.json

OKR: okr-l4-agt-bridge KR1 (compiler implementation).
"""

__version__ = "0.1.0-dev"
