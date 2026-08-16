"""Cognitive Heartbeat Control Loop — User Guide

## Overview

The cognitive heartbeat is a predict-then-declare gate that prevents unratified claims
from being emitted as complete responses. When you make a claim (keywords: DONE, COMPLETE,
VERIFIED, WORKING, FIXED, SHIPPED), the system validates that you've pre-declared the
prediction with:

  1. **Prediction**: what you're claiming (e.g., "NERVE chain is COMPLETE")
  2. **Falsifier**: empirical observation that would disprove the claim
  3. **Substrate**: measurement environment where verification occurred
  4. **Budget**: remaining turns before the prediction is considered stale

## When to Use cognitive_predict()

Call `cognitive_predict()` BEFORE emitting any response that contains claim keywords.

### Example 1: Verifying a Fix Works

```python
# First declare the prediction
cognitive_predict(
    prediction="The bug fix is COMPLETE and tests pass",
    falsifier="Any test fails on main",
    substrate="bare-repo-main",
    budget=2,
)

# Then emit your response (OK — prediction is recorded)
"The NERVE chain is now COMPLETE. All 143 regression tests pass."
```

### Example 2: Production Deployment Claim

```python
# Declare with production substrate
cognitive_predict(
    prediction="Code SHIPPED to production",
    falsifier="Health check alert fires within 5 minutes",
    substrate="production-service",
    budget=5,  # Give it 5 turns to monitor
)

"The SHIPPED code is live on production."
```

### Example 3: Multiple Claims in Same Turn

```python
# If you have two independent claims, predict each one
cognitive_predict(
    prediction="Tests COMPLETE on CI",
    falsifier="CI build fails",
    substrate="running-gateway",
    budget=1,
)

cognitive_predict(
    prediction="Code review VERIFIED",
    falsifier="Reviewer requests changes",
    substrate="h2-tree",
    budget=2,
)

"The tests are COMPLETE, and code review VERIFIED."
```

## Substrate Options

Choose the substrate that matches where you actually verified the claim:

| Substrate | When to Use |
|-----------|------------|
| `bare-repo-main` | Verified on a clean clone of the repo |
| `python-repl-fresh-import` | Ran Python code in a fresh interpreter |
| `running-gateway` | Tested against the live gateway/server |
| `h1-tree` | Verified in the h1 (source-of-truth) tree |
| `h2-tree` | Verified in h2 (downstream) tree |
| `production-service` | Verified on production service |

**Important**: Substrate-hops require new predictions. If you verified on `bare-repo-main`,
you cannot later claim the same thing is "VERIFIED on production-service" without a new
prediction that accounts for the drift.

## Budget and Staleness

Each prediction has a **budget** — number of remaining turns before it expires:

- **Budget 0**: Prediction is stale and will be flagged
- **Budget 1-3**: Active prediction, needs ratification or reprobe within N turns
- **Budget 5+**: Long-running claim (e.g., production deployment with 5-turn monitoring window)

The system decrements budget at turn boundaries. A prediction with budget=0 cannot support
a claim — you must either:
  1. **Ratify it** (`cognitive_predict` with same claim + fresh budget), or
  2. **Reprobe it** (call `cognitive_predict` again with new evidence)

## What Happens When Gaps Are Detected

If you claim COMPLETE but didn't call `cognitive_predict()`, the system injects a soft warning:

```
⚠️  Claim detected without pre-declared prediction. Use cognitive_predict(prediction, 
falsifier, substrate, budget) before declaring DONE/COMPLETE/VERIFIED. Confidence downgraded.

[Your original response continues below...]
```

The warning is **non-blocking** — your response still reaches the user, but the gap is flagged
so you can adjust future behavior.

## Integration with Kanban / Long-Running Tasks

For kanban workers claiming task completion:

```python
# BEFORE kanban_complete, declare the prediction
cognitive_predict(
    prediction="VFE-NERVE-01 task COMPLETE and merged to main",
    falsifier="CI regression detected after merge",
    substrate="bare-repo-main",
    budget=3,
)

# Then call kanban_complete
kanban_complete(
    summary="VFE-NERVE-01 merged: typed block/complete envelopes now live",
    metadata={...},
)
```

This makes the prediction visible in the cognitive audit trail so the dispatcher can
cross-reference task completion with empirical verification events.

## Observability

All heartbeat inspections are logged to `agent.log` at INFO level:

```
cognitive_heartbeat: ⚠️  Prediction recorded but incomplete (1 gap(s)): 
prediction[0]: empty falsifier — prediction_details=...
```

Metrics (Phase 2):
- `cognitive_predictions_open` — current count of open predictions
- `cognitive_predictions_ratified_total` — cumulative ratified predictions
- `cognitive_predictions_falsified_total` — disproven predictions
- `cognitive_heartbeat_interventions_total` — soft warnings injected

## Disabling the Heartbeat (Troubleshooting)

If the heartbeat is interfering with expected behavior, disable it temporarily:

```yaml
# ~/.hermes/config.yaml
cognitive:
  heartbeat:
    enabled: false  # Turn off pre-emit gate
```

## Frequently Asked Questions

### Q: Does heartbeat block my response?
**A:** No. It prepends a soft warning but never blocks emit. Your response always reaches the user.

### Q: What if I forgot to call cognitive_predict before claiming?
**A:** You'll see a warning. Call `cognitive_predict` in your next turn to declare the claim retroactively, and the system will accept it.

### Q: Can I have multiple open predictions in one turn?
**A:** Yes. Call `cognitive_predict` multiple times for independent claims. Each gets tracked separately.

### Q: What's a "falsifier"?
**A:** It's an empirical observation that would prove your claim wrong. For "code is COMPLETE":
the falsifier might be "tests fail" or "CI build breaks". It forces you to think about what
evidence would disprove the claim before you make it.

### Q: How do I know when a prediction is stale?
**A:** The heartbeat validates budget > 0 for any claim. If you see "budget expired (0)"
in a warning, you need to:
  1. Verify the claim again in a fresh turn, or
  2. Update the budget: call `cognitive_predict` again with the same prediction but budget=3

### Q: Do I need to ratify predictions explicitly?
**A:** Not for this phase. Phase 2 adds explicit ratification. For now, predictions are 
tracked automatically and you can reference them via `cognitive_recall`.

## Example Workflow

```
Turn 1: Run tests
├─ cognitive_predict("Tests PASS", "Test fails", "bare-repo-main", budget=2)
└─ Response: "I ran the test suite and all tests PASS."

Turn 2: Merge to main
├─ cognit ive_predict("Code merged to main", "Merge fails", "bare-repo-main", budget=3)
└─ Response: "Code is now merged and WORKING on main."

Turn 3: Deploy to prod
├─ cognitive_predict("Deployed to prod", "Health check fires", "production-service", budget=5)
└─ Response: "SHIPPED to production. Health checks green."

Turn 4: Monitor (no new claims, budget decrements)
└─ Response: "Monitoring in progress. No alerts."

Turn 5: Incident? Reprobe
├─ cognitive_predict("Still working?", "Incident detected", "production-service", budget=3)
└─ Response: "Spot check shows no issues. Still WORKING."
```

## See Also

- `cognitive_decide()` — log reasoning decisions (separate from predictions)
- `cognitive_recall()` — search past decisions and predictions
- Agent memory documentation for context injection and prefetch
"""
