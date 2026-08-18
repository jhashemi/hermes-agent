"""
Executive Agent Activation Plugin for Hermes.

Wires together all 4 KRs as a pre_gateway_dispatch hook and registers
the activation tools (executive_resolve, executive_activate) for use
in the agent's tool loop.

KR1: resolver.resolve_active_agent() — resolves which agent is active
KR2: cognitive_memory.query_cognitive_memory() — queries agent's cognitive memory
KR3: resolver._raci_resolve() — RACI fallback when no agent is active
KR4: activation_cycle.run_activation_cycle() — full cognitive cycle
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .resolver import resolve_active_agent, AGENTS
from .activation_cycle import run_activation_cycle

logger = logging.getLogger(__name__)

# Per-user session state: {user_id: persona_id}
_active_agents: Dict[str, str] = {}


def _get_session_agent(user_id: str) -> Optional[str]:
    return _active_agents.get(str(user_id))


def _set_session_agent(user_id: str, persona_id: str) -> None:
    _active_agents[str(user_id)] = persona_id


# ── Tool schemas ──────────────────────────────────────────────────────────

RESOLVE_SCHEMA = {
    "name": "executive_resolve",
    "description": (
        "Resolve the active executive agent (helios/Elon, atlas/Jobs, orion/Demis) "
        "from the current command and conversation context. Returns the resolved agent, "
        "confidence, and reasoning. Use before responding to domain-specific queries to "
        "determine which executive persona should lead the response."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The user's command or query to resolve an agent for.",
            },
            "hint": {
                "type": "string",
                "description": "Optional hint about the domain or preferred agent.",
            },
        },
        "required": ["command"],
    },
}

ACTIVATE_SCHEMA = {
    "name": "executive_activate",
    "description": (
        "Activate an executive agent for the current session. Runs a full "
        "observe->reason->plan->reward->memory->reflect cognitive cycle and "
        "queries the agent's cognitive memory. Returns activation context to "
        "inject into the response. Use after executive_resolve to fully activate "
        "the agent and retrieve their relevant memories."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command/query triggering activation.",
            },
            "persona_id": {
                "type": "string",
                "description": "The persona to activate: helios, atlas, or orion.",
                "enum": ["helios", "atlas", "orion"],
            },
        },
        "required": ["command"],
    },
}


# ── Tool handlers ─────────────────────────────────────────────────────────

def handle_executive_resolve(command: str, hint: str = "", user_id: str = "") -> dict:
    """Handle executive_resolve tool call."""
    current_agent = _get_session_agent(user_id) if user_id else None

    ctx = resolve_active_agent(
        command=command + (" " + hint if hint else ""),
        current_session_agent=current_agent,
    )

    result = {
        "persona_id": ctx.persona_id,
        "full_name": ctx.full_name,
        "confidence": ctx.confidence,
        "reason": ctx.reason,
        "via_raci": ctx.via_raci,
    }

    if ctx.persona_id:
        logger.info(
            "[activation] Resolved %s (conf=%.2f, raci=%s): %s",
            ctx.persona_id, ctx.confidence, ctx.via_raci, ctx.reason
        )

    return result


def handle_executive_activate(
    command: str,
    persona_id: Optional[str] = None,
    user_id: str = "",
) -> dict:
    """Handle executive_activate tool call."""
    # Resolve if no persona_id given
    if persona_id and persona_id in AGENTS:
        info = AGENTS[persona_id]
        from .resolver import ActivationContext, _load_profile
        ctx = ActivationContext(
            persona_id=persona_id,
            full_name=info["full_name"],
            agent_dir=info["agent_dir"],
            confidence=0.9,
            reason=f"explicit activation: {persona_id}",
            profile=_load_profile(info["agent_dir"]),
        )
    else:
        current_agent = _get_session_agent(user_id) if user_id else None
        ctx = resolve_active_agent(command=command, current_session_agent=current_agent)

    if not ctx.persona_id:
        return {"error": "Could not resolve an active agent"}

    # Run the full cognitive cycle (KR4)
    result = run_activation_cycle(ctx=ctx, command=command)

    # Update session state
    if user_id:
        _set_session_agent(user_id, result.persona_id)

    return {
        "activation_id": result.activation_id,
        "persona_id": result.persona_id,
        "full_name": result.full_name,
        "confidence": result.confidence,
        "via_raci": result.via_raci,
        "cycle_steps": [s.step for s in result.cycle],
        "injected_context": result.injected_context[:500] if result.injected_context else "",
        "memory_records": len([s for s in result.cycle if s.step == "memory"]),
    }


# ── Gateway dispatch hook ─────────────────────────────────────────────────

def pre_gateway_dispatch_hook(event: Any, gateway: Any = None, session_store: Any = None, **kwargs) -> Optional[Dict]:
    """
    Pre-dispatch hook for executive agent activation.

    Intercepts:
    - /executive-resolve <command>  — resolve active agent for a command
    - /executive-activate [persona]  — activate agent and run cognitive cycle
    - /executive-status  — show current active agent

    For all other messages: silently resolves agent in background (no skip).
    """
    try:
        # ``event`` is a gateway.platforms.base.MessageEvent dataclass, NOT a
        # dict. Access via attributes; ``event.source`` (a SessionSource) is
        # where user_id lives. Fall back to duck-typed dict access so unit
        # tests that pass raw dicts continue to work.
        if hasattr(event, "text"):
            text = (getattr(event, "text", "") or "").strip()
            source = getattr(event, "source", None)
            user_id = str(getattr(source, "user_id", "") or "") if source else ""
        else:  # dict-shaped fallback (legacy tests / synthetic callers)
            msg = event.get("message", {}) if hasattr(event, "get") else {}
            text = (msg.get("text") or msg.get("body") or "").strip()
            user_id = str(event.get("user_id") or event.get("from") or "") if hasattr(event, "get") else ""

        if not text:
            return None

        # ── /executive-resolve ─────────────────────────────────────────
        if text.lower().startswith("/executive-resolve "):
            command = text[len("/executive-resolve "):].strip()
            result = handle_executive_resolve(command=command, user_id=user_id)
            reply = (
                f"🎯 Executive Agent Resolved\n"
                f"Agent: {result.get('full_name', '?')} ({result.get('persona_id', '?')})\n"
                f"Confidence: {result.get('confidence', 0):.0%}\n"
                f"Via RACI: {result.get('via_raci', False)}\n"
                f"Reason: {result.get('reason', '')}"
            )
            return {"action": "reply", "text": reply}

        # ── /executive-activate ────────────────────────────────────────
        if text.lower().startswith("/executive-activate"):
            parts = text.split(maxsplit=1)
            persona_hint = parts[1].strip() if len(parts) > 1 else ""
            persona_id = persona_hint if persona_hint in AGENTS else None
            result = handle_executive_activate(
                command=persona_hint or "activate agent",
                persona_id=persona_id,
                user_id=user_id,
            )
            steps = " → ".join(result.get("cycle_steps", []))
            reply = (
                f"⚡ Executive Agent Activated\n"
                f"Agent: {result.get('full_name', '?')} ({result.get('persona_id', '?')})\n"
                f"Activation ID: {result.get('activation_id', '?')}\n"
                f"Cognitive Cycle: {steps}\n"
                f"Confidence: {result.get('confidence', 0):.0%}\n"
                f"Via RACI: {result.get('via_raci', False)}"
            )
            return {"action": "reply", "text": reply}

        # ── /executive-status ──────────────────────────────────────────
        if text.lower() in ("/executive-status", "/executive-agents"):
            current = _get_session_agent(user_id)
            if current and current in AGENTS:
                info = AGENTS[current]
                reply = f"Active executive: {info['full_name']} ({current})"
            else:
                reply = "No executive agent active. Use /executive-resolve <command> to resolve one."
            return {"action": "reply", "text": reply}

        # ── Background resolution for all other messages ───────────────
        # Silently resolve and log (no skip — Hermes still processes)
        if len(text) > 10:
            current_agent = _get_session_agent(user_id)
            ctx = resolve_active_agent(command=text, current_session_agent=current_agent)
            if ctx.persona_id and ctx.confidence >= 0.6:
                _set_session_agent(user_id, ctx.persona_id)
                logger.debug(
                    "[activation] Background resolved %s for user %s (conf=%.2f)",
                    ctx.persona_id, user_id, ctx.confidence,
                )

        return None  # Don't skip — let Hermes process normally

    except Exception as e:
        logger.warning("[activation] Hook error: %s", e, exc_info=True)
        return None


# ── Plugin registration ───────────────────────────────────────────────────

def register(ctx) -> None:
    """Register the executive-activation plugin with Hermes."""
    logger.info("[activation] Registering executive-activation plugin")

    # Register the pre_gateway_dispatch hook
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch_hook)
        logger.info("[activation] Registered pre_gateway_dispatch hook")
    elif hasattr(ctx, "hooks"):
        ctx.hooks["pre_gateway_dispatch"] = pre_gateway_dispatch_hook
        logger.info("[activation] Registered hook via ctx.hooks")

    # Register tools if ctx supports it
    if hasattr(ctx, "register_tool"):
        try:
            ctx.register_tool(
                schema=RESOLVE_SCHEMA,
                handler=lambda args: handle_executive_resolve(
                    command=args.get("command", ""),
                    hint=args.get("hint", ""),
                ),
            )
            ctx.register_tool(
                schema=ACTIVATE_SCHEMA,
                handler=lambda args: handle_executive_activate(
                    command=args.get("command", ""),
                    persona_id=args.get("persona_id"),
                ),
            )
            logger.info("[activation] Registered executive_resolve and executive_activate tools")
        except Exception as e:
            logger.warning("[activation] Could not register tools: %s", e)
