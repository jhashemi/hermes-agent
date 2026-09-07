"""Hexagonal ports for kanban dispatch.

This package exposes two distinct ports that factorize the dispatcher's
Plan/Free logic into separate hexagonal shells:

    PlanPort  — value iteration on a learned scoring function G.
                This is the *policy* side: given a set of candidates,
                rank/select which task to dispatch next.

    FreePort  — tree-unfold over the task DAG.
                This is the *structure* side: given a set of ready root
                task ids, traverse the dependency graph and return the
                candidate subtree metadata.

The two ports share NO state. They communicate only through immutable
DTOs (``CandidateTask``, ``SubtreeInfo``). The integration layer
(``dispatch_once_free``) calls FreePort first (unfold), then PlanPort
(rank/select), then dispatches the winner.

Gap G-S5-H3-1 closure: this factorization is the structural evidence that
``Plan`` and ``Free`` are separated.
"""

from hermes_kanban.ports.plan_free_ports import (
    # DTOs
    CandidateTask,
    SubtreeInfo,
    # Port protocols
    FreePort,
    PlanPort,
    # Concrete adapters
    BFSFreeAdapter,
    RICEPlanAdapter,
    # Documented integration adapter
    PlanFreeDispatchAdapter,
    # Factory
    make_default_plan_free_adapter,
)

__all__ = [
    "CandidateTask",
    "SubtreeInfo",
    "FreePort",
    "PlanPort",
    "BFSFreeAdapter",
    "RICEPlanAdapter",
    "PlanFreeDispatchAdapter",
    "make_default_plan_free_adapter",
]
