# Per-System Policy Catalog Schema

Every autonomous system + core infra component declares a 4-axis policy bundle.
Source-of-truth: `~/.hermes/policies/<system>/{governance,runtime,integration,usage}.l4`
Compiled artifact: `~/.hermes/policies/<system>/.compiled/<axis>.corel4.json`
Audit log: `~/.hermes/policies/.audit/<system>.<axis>.log`

## Axis definitions

### 1. governance.l4 — WHO can change me + signoff
Answers: who is allowed to modify this system's config? what signoff is required?
who owns this system? when must the council be involved?

Required RULES:
- `<system>-config-change` — required signoffs to mutate config
- `<system>-restart-authority` — who can stop/start the service
- `<system>-policy-update` — how this very policy bundle gets updated
- `<system>-emergency-override` — break-glass with mandatory post-incident review

### 2. runtime.l4 — invariants the running system MUST maintain
Answers: what must always be true while this is running? what are the SLOs?
when is the system "degraded" vs "critical"?

Required RULES:
- `<system>-vein-emit-sla` — health envelope must publish every N seconds
- `<system>-loop-lag-bound` — p99 inner-loop latency MUST < threshold
- `<system>-error-rate-bound` — error_rate_60s MUST < threshold
- `<system>-deps-required` — upstream deps that MUST be reachable

### 3. integration.l4 — what I can pub/sub + with what auth
Answers: which NATS subjects can this system publish to? subscribe from?
what headers are required? what message shapes are valid?

Required RULES:
- `<system>-publish-allow` — allowed publish subjects + required headers
- `<system>-subscribe-allow` — allowed subscribe subjects
- `<system>-message-shape` — required JSON schema (or pointer)
- `<system>-evidence-required` — when does this system need evidence headers
  (esp. `Nats-Msg-Id`, `X-Hermes-Signoff`, `X-Hermes-Evidence-Uri`)

### 4. usage.l4 — quotas, rate limits, cost ceilings, who may invoke
Answers: who can call me? how often? at what cost? what's the budget?

Required RULES:
- `<system>-rate-limit` — N actions per window
- `<system>-quota-daily` — N actions per UTC day
- `<system>-cost-ceiling` — max $ or token spend per window
- `<system>-allowed-callers` — actor allowlist (ties to LLDAP roles)

## Authoring workflow

```
       ┌─────────────────────────┐
1.     │ Operator describes rule │
       │   in plain English       │
       └─────────────┬───────────┘
                     ▼
       ┌─────────────────────────┐
2.     │ GPT-4o structured       │
       │ extraction → L4 AST     │
       └─────────────┬───────────┘
                     ▼
       ┌─────────────────────────┐
3.     │ jl4-service /validate   │
       │ (REST in Docker)        │
       └─────────────┬───────────┘
                     ▼
       ┌─────────────────────────┐
4.     │ Council quorum check    │
       │ (T1=3of3, T2=2of3, T3=1)│
       └─────────────┬───────────┘
                     ▼
       ┌─────────────────────────┐
5.     │ AgentOS PolicyEngine    │
       │ register_policy(...)    │
       └─────────────┬───────────┘
                     ▼
       ┌─────────────────────────┐
6.     │ AgentMesh enforcer      │
       │ consults on every msg   │
       └─────────────────────────┘
```

## Tier classification

| Tier | Axes | Example | Required signoff |
|---|---|---|---|
| T1 | governance.l4 (substrate) | NATS cluster, LLDAP, AgentOS itself | Council 3-of-3 |
| T2 | runtime.l4, integration.l4 | per-reactor invariants | 2-of-3 council |
| T3 | usage.l4 | rate limits, quotas | Single author + audit |

## File header (required)

Every `.l4` file MUST start with:

```l4
-- system: <system-name>
-- axis: <governance|runtime|integration|usage>
-- tier: <T1|T2|T3>
-- author: <agent-id>
-- ratified: <ISO-8601 or PENDING>
-- council_signoffs: [<id>, <id>, ...]
-- supersedes: <prior-policy-id or NONE>
-- version: <semver>
```

## Compilation pipeline

1. `tools/policy_catalog/compile.py <system> <axis>` →
2. `docker run jl4-service /validate < <axis>.l4` →
3. write CoreL4 JSON to `.compiled/<axis>.corel4.json` →
4. POST to AgentOS PolicyEngine `/register` →
5. on success, append to audit log + emit `policy.registered.<system>.<axis>` NATS event.

## Validation gates

- **Lint**: every required RULE present, header complete.
- **Conflict check**: PolicyEngine cross-axis dry-run; reject if a `runtime.l4` invariant is unsatisfiable given `integration.l4` constraints.
- **Coverage**: every NATS subject this system publishes/subscribes to MUST appear in `integration.l4`. Build-time grep generates a TODO list.
- **Sunset**: policies with `version` >12 months old without re-ratification trigger a `policy.stale.<system>` alert.
