"""Kernel-side shim: re-exports the cluster-dispatch policy body from
``eap.cluster_dispatch`` (L3 platform layer).

The full 716-line implementation lives in the Executive Agents Platform
(EAP) repo. This shim keeps kernel import sites
(``hermes_cli/kanban_db.py``, ``hermes_cli/doctor.py``,
``gateway/kanban_watchers.py``) unchanged so the ``dispatch_once``
seam remains the only L1 primitive.

Fail-loud on missing EAP: dispatch silently reverting to local placement
because of a broken EAP install is a placement-integrity bug worse than
a hard ImportError.

Ticket: t_03224c92 (Option C, board-ratified 2/2 by jeff_dean + elon_musk).
Follow-up: Option B (register_remote_spawner plugin hook) will remove
this shim.
"""

try:
    from eap.cluster_dispatch import (  # noqa: F401
        _NODE_HOSTS,
        compute_out_of_scope_boards,
        create_cluster_node_router,
        local_node_router,
        log_out_of_scope_boards_at_startup,
        spawn_on_remote,
    )
except ImportError as exc:  # pragma: no cover - fail-loud by design
    raise ImportError(
        "gateway.cluster_dispatch requires eap.cluster_dispatch (EAP L3 "
        "platform). Install executive-agents-platform into this environment."
    ) from exc
