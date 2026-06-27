# ADR-011 Architectural Review — Differentiable RSI & Reward-Shaping Lens

**Reviewer:** Demis Hassabis (council seat)
**Date:** 2026-06-06
**ADR under review:** ADR-011 — HRV-OTEL Substrate, Differentiable RSI, Per-System Policy Catalog
**Scope of this review:** §2.3 (Differentiable RSI), §3.2/§3.3 (SGD risks, shadow baseline), §6 Q3 (weight init), overall reward structure.

---

## 1. Conceptual Framing — What ADR-011 Is Really Proposing

At its core, §2.3 defines a **continuous-state, continuous-action control problem** dressed as a scheduler: observe `x_t ∈ ℝ⁷`, emit `a_t = π_θ(x_t)`, receive `y_t` one hour later, minimize a weighted composite loss. That is a delayed-reward RL problem with a one-hour credit-assignment horizon — not a supervised regression problem. Framing it as "SGD on a loss" is a useful simplification for Phase 2, but the authors should be explicit about the gap between the optimization proxy they are implementing and the true objective. Precision here matters because the wrong framing produces the wrong escalation criteria.

---

## 2. SGD on Small-Data Noise

**The stated mitigation (≥50 cycles before applying updates) is necessary but not sufficient.** At a nominal 60 s tick, 50 observations = 50 minutes of wall-clock data. θ has ≈20 free parameters; the effective sample size after accounting for the 1-hour outcome lag is roughly 1 labeled example per hour of operation. The per-parameter SNR at initialization will be dominated by process noise in `completion_rate`, `error_rate`, `lock_wait`, and `fabrication_count` — all of which co-vary with external events (deployments, human work patterns, NATS jitter) that are uncorrelated with θ.

**Recommended additions:**
- **Exponential moving average (EMA) smoothing on y_t** before computing ∇L. Window ≥ 5 cycles reduces variance without introducing lag bias at the learning-rate timescale.
- **Trust-region constraint on each update step**: ‖Δθ‖ ≤ δ_max (e.g., 0.05 per parameter per step), not just post-hoc clipping. This is more principled than η=0.01 + momentum=0.9, which can compound to large effective steps during the initial noisy phase.
- **Minimum data requirement should be framed in terms of effective independent samples**, not raw cycle count. If many cycles share the same system state (veins all healthy, nothing changing), they add almost zero information. Consider a novelty-weighted count.

---

## 3. Loss Weight Initialization w₁..w₄

**This is the highest-risk unresolved item in the entire ADR.** The loss is:

```
L(θ) = w₁·(1 − completion_rate) + w₂·error_rate + w₃·fabrication_count + w₄·avg_lock_wait + λ·‖θ‖²
```

The four terms are **not dimensionally homogeneous**:
- `completion_rate` ∈ [0, 1] (dimensionless fraction)
- `error_rate` ∈ [0, ∞) errors/sec
- `fabrication_count` ∈ {0, 1, 2, ...} (integer events)
- `avg_lock_wait` ∈ [0, ∞) milliseconds

Summing these without normalization means the gradient direction is determined by whichever term happens to have the largest numerical range at initialization — almost certainly `avg_lock_wait` in milliseconds. **The optimizer will effectively ignore fabrication_count and error_rate until lock_wait is near zero**, regardless of what the weights nominally say.

**Required before Phase 2:**
1. Normalize each term by its empirical standard deviation over the warm-up period (standardize to z-score) so gradient magnitudes are comparable across dimensions.
2. Choose w₁..w₄ from a principled prior: e.g., rank-order the terms by business consequence (fabrication_count should almost certainly dominate), then set weights proportionally. Document the rationale.
3. Q3 (§6) correctly asks whether the weights should themselves be meta-learned. The answer is yes in the long run, but **do not defer this to Phase 2 implicitly while treating the initial guesses as valid for gradient descent**. Treating unspecified weights as valid hyperparameters is the same epistemological error that produced the OKR fabrication: assumption of correctness without evidence.

---

## 4. Finite-Diff vs. Autograd

The plan is: FD with ~20 evals/cycle initially; upgrade to JAX autograd later. **The "~20 evals/cycle" claim requires scrutiny.**

Each true evaluation of `∂L/∂θ_i` via central differences requires observing `y_t` at `(θ + εeᵢ)` and `(θ − εeᵢ)`. With a 1-hour outcome lag and 20 parameters, exhaustive FD estimation requires 40 separate 1-hour evaluation windows — roughly **40 hours per gradient step** if run serially. This is impractical.

What the authors almost certainly mean is: run the current π_θ, observe outcomes, then compute a **surrogate gradient** using a local linear model fitted to recent (x_t, a_t, y_t) tuples. That is not finite differences — that is **fitted-value policy gradient** or a basic form of model-based RL. The distinction matters for correctness proofs, convergence guarantees, and bias analysis.

**My recommendation:** Be explicit. The right approach for Phase 2 is:
- Fit a lightweight surrogate model `f̂(a_t | x_t; θ) → ŷ_t` from logged data (linear for now).
- Compute ∇_θ L via autograd through f̂ (not true FD).
- Periodically re-fit f̂ as more (x, a, y) triples accumulate.
- When the surrogate model is good enough, the gradient is reliable; when it is not, the trust-region constraint absorbs the error.
- **Skip the FD phase entirely** — it is neither cheap nor correct for delayed outcomes.

---

## 5. Exploration vs. Exploitation

The current formulation is **pure exploitation from the first update step**. η=0.01, momentum=0.9, gradient descent — no stochasticity in action selection beyond whatever `θ_policy_mutation_rate` controls (which is itself a parameter being optimized, creating a circular dependency between exploration policy and its own optimizer).

At 19 systems with sparse coverage of the state space, the agent will rapidly converge to a local basin that looks good on the warm-start heuristics but has never been tested away from that region. This is particularly dangerous for `θ_council_quorum_required` and `θ_evidence_gate_strictness` — parameters with non-convex, threshold-like effects on `fabrication_count`.

**Required additions:**
- Inject additive Gaussian noise `ξ ~ N(0, σ²_explore)` into `a_t` during the first 100 cycles, decayed by a schedule analogous to simulated annealing.
- Treat `θ_policy_mutation_rate` and `θ_policy_mutation_temp` as **fixed hyperparameters** (not optimized) until 500+ cycles, to avoid the optimizer discovering it can suppress exploration to lower short-term loss.

---

## 6. Reward Gaming Risk — Goodhart's Law on completion_rate

**This is the most structurally dangerous term in the loss, and the ADR is insufficiently alarmed about it.**

The system recently post-mortemed an OKR fabrication event caused by `okr_kr_reconciler.py` bypassing the evidence gate. The proposed fix is L4 policy enforcement. Simultaneously, ADR-011 proposes to reward the RSI optimizer on `w₁·(1 − completion_rate)`.

**These two objectives are in direct tension.** If the optimizer can increase `completion_rate` by relaxing `θ_evidence_gate_strictness`, it will. If `fabrication_count` is only counted when the PolicyEngine catches the bypass, and the optimizer also controls `θ_council_quorum_required` (which affects how thoroughly the policy is audited), then **the reward signal is not independent of the system being controlled**. This is textbook Goodhart: the moment completion_rate becomes the optimization target, it ceases to be a reliable measure of genuine task completion.

**Required structural safeguard:**
- `fabrication_count` and `completion_rate` must be measured by a **logically isolated auditor** that the RSI optimizer has zero control over. The audit stream must not pass through any NATS path that `θ_integration` parameters can influence.
- Consider removing `completion_rate` from the primary loss entirely and replacing it with **evidence-gated completion rate** (completions that passed the full PolicyEngine check) measured by the auditor. This aligns the optimization target with the actual governance objective.

---

## 7. Shadow Baseline Isolation

R3 (§3.3) proposes: "a separate non-SGD baseline policy runs in shadow; if the optimized θ underperforms baseline by >20% for 3 cycles, revert."

Two problems:

**Contamination:** The shadow baseline and the live θ observe the same NATS traffic, the same system state, and the same `y_t` outcomes. If the live θ is making decisions that alter system behavior (even slightly — e.g., changing RSI cycle intervals affects queue depth, which affects `veins_healthy_frac`), then the shadow baseline's counterfactual performance estimate is confounded. This is not a shadow — it is a correlated co-observer. True isolation requires running the baseline's recommended actions in a separate time window or a separate cluster partition (even a synthetic replay).

**Revert threshold of 20% over 3 cycles is statistically underpowered:** With the noise levels described in §3.2, a 20% difference over 3 cycles is well within sampling variance for most outcome metrics. The revert trigger will either fire spuriously (undoing valid progress) or fail to fire (allowing bad convergence to persist). Suggest: replace with a **sequential probability ratio test (SPRT)** or a **Bayesian credible interval** — halt-and-revert only when P(θ_live < θ_baseline) > 0.95 with evidence from ≥ 10 cycles.

---

## 8. When to Escalate to MLP / Full RL

The ADR mentions "upgrade to MLP after baseline" as a single sentence. This is under-specified. My recommended escalation criteria:

| Signal | Action |
|--------|--------|
| Linear `π_θ` residuals show systematic nonlinearity (R² < 0.6 on held-out cycles) | Add hidden layer (width 32); use JAX autograd |
| Optimal θ is at the boundary of its clip range for any parameter | Expand the action space; reconsider parameterization |
| `fabrication_count > 0` after full policy catalog rollout | Halt SGD; audit reward signal for gaming before resuming |
| Surrogate model prediction error > 30% for 10 consecutive cycles | Switch to model-based RL (Dyna-style); the world model needs explicit learning |
| θ converges but system KPIs plateau | The loss function is the problem, not the optimizer — escalate to council for reward redesign |

Do not escalate to MLP simply because it "should be better." The linear policy is interpretable and auditable; the upgrade cost in governance overhead is real.

---

## Summary of Required Changes Before APPROVE

| # | Issue | Severity | Action Required |
|---|-------|----------|----------------|
| 1 | w₁..w₄ dimensionally inhomogeneous | **Critical** | Normalize terms; document weight rationale |
| 2 | completion_rate in loss creates Goodhart loop | **Critical** | Replace with auditor-isolated evidence-gated completion; auditor must be outside θ control |
| 3 | FD gradient semantics mismatch with 1-hour delayed outcomes | **High** | Reframe as surrogate-model gradient; skip FD phase |
| 4 | No exploration mechanism | **High** | Add annealed action noise; freeze mutation_rate during bootstrap |
| 5 | Shadow baseline not causally isolated | **High** | Redefine as SPRT-gated; document confounding |
| 6 | SGD update without EMA smoothing | **Medium** | Add EMA on y_t; use trust-region step bound |
| 7 | MLP/RL escalation criteria absent | **Medium** | Add explicit criteria (see §8 above) |
| 8 | w₁..w₄ meta-optimization deferred without plan | **Medium** | Must be scheduled before Phase 3 |

---

**VERDICT: APPROVE_WITH_REVISIONS**

The architectural instinct here is sound — biologically-inspired closed-loop observability with differentiable optimization of the control policy is the right direction, and demoting cron monitors to substrate watchdogs is clearly correct. The L4 policy enforcement layer is a necessary structural control for the fabrication problem. But the differentiable RSI specification as written contains a potential reward-gaming loop that could recreate the very failure mode we are trying to prevent, and the gradient estimation plan is not coherent for delayed-outcome settings. Items 1 and 2 in the table above must be resolved before Phase 2 begins. All eight items must be resolved before Phase 4 sign-off.
