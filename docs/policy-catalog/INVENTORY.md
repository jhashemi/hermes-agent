# Policy Catalog Authoring Inventory

24 systems × 4 axes = **96 L4 files** to author.
Track progress here. As each file lands, mark `[x]`.

## Tier-1 (substrate, council 3-of-3 required)

These are the foundations everything else stands on.

| System | Governance | Runtime | Integration | Usage |
|---|---|---|---|---|
| nats-cluster | [ ] | [ ] | [ ] | [ ] |
| lldap | [ ] | [ ] | [ ] | [ ] |
| agent-os (PolicyEngine) | [ ] | [ ] | [ ] | [ ] |
| agent-mesh (MessageBroker) | [ ] | [ ] | [ ] | [ ] |
| jl4-service | [ ] | [ ] | [ ] | [ ] |
| hrv-pacemaker | [ ] | [ ] | [ ] | [ ] |
| rsi-optimizer | [ ] | [ ] | [ ] | [ ] |

## Tier-2 (per-reactor invariants, council 2-of-3)

| System | Governance | Runtime | Integration | Usage |
|---|---|---|---|---|
| consensus-reactor | [x] | [x] | [x] | [x] |
| okr-dispatch-reactor | [ ] | [ ] | [ ] | [ ] |
| okr-kr-reconciler | [ ] | [ ] | [ ] | [ ] |
| okr-steering-reactor | [ ] | [ ] | [ ] | [ ] |
| event-emitter-omnibus | [ ] | [ ] | [ ] | [ ] |
| kr-watchdog | [ ] | [ ] | [ ] | [ ] |
| org-chart-router | [ ] | [ ] | [ ] | [ ] |
| hermes-gateway | [ ] | [ ] | [ ] | [ ] |
| hermes-skills-broadcast | [ ] | [ ] | [ ] | [ ] |
| cluster-broadcast-listener | [ ] | [ ] | [ ] | [ ] |
| voice-bridge | [ ] | [ ] | [ ] | [ ] |
| vcg-crdt-bridge | [ ] | [ ] | [ ] | [ ] |
| serena-lsp | [ ] | [ ] | [ ] | [ ] |
| memory-recorder | [ ] | [ ] | [ ] | [ ] |
| github-webhook-bridge | [ ] | [ ] | [ ] | [ ] |
| agent-actors | [ ] | [ ] | [ ] | [ ] |

## Tier-3 (developer / op tools, single-author)

| System | Governance | Runtime | Integration | Usage |
|---|---|---|---|---|
| novnc | [ ] | [ ] | [ ] | [ ] |
| x11vnc | [ ] | [ ] | [ ] | [ ] |
| xvfb | [ ] | [ ] | [ ] | [ ] |

## Authoring order (recommended)

**Week 1**: Tier-1 substrate (7 systems × 4 = 28 files). Council reviews in batches.
**Week 2-3**: Tier-2 reactors (16 systems × 4 = 64 files). 1/day pace.
**Week 4**: Tier-3 dev tools (3 systems × 4 = 12 files). Solo author.

## Per-file authoring template

For every system, the four files **must** be authored in this order to avoid
chicken-and-egg:

1. `governance.l4` first — establishes who can author the rest.
2. `integration.l4` — what subjects exist; constrains what runtime can observe.
3. `runtime.l4` — invariants over the integration surface.
4. `usage.l4` — quotas/limits, last because they're tunable.

## Cross-cutting concerns (NOT per-system)

These live at `~/.hermes/policies/_global/`:

- `evidence-headers.l4` — every consensus.decision.> publish needs evidence
- `nats-msg-id-required.l4` — all publishers must include `Nats-Msg-Id`
- `okr-fabrication-prevention.l4` — bare UPDATE on okr_accountability.db is REJECTED
  unless wrapped in `OKRAccountabilitySystem.update_key_result()` call path
- `lldap-role-resolution.l4` — every actor reference must resolve to LLDAP
- `audit-ledger-append-only.l4` — Merkle-hashed append-only log
