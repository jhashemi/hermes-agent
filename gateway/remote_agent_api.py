"""Remote agent execution API endpoint for multi-instance Hermes.

Each Hermes instance exposes an HTTP API that allows remote instances to:
  POST /api/agent/execute       - Run a prompt and get response
  GET /health                   - Health check
  GET /api/agent/status         - Current agent status

This allows the WhatsApp gateway (44.198.134.0) to dispatch requests to
the agent execution layer (hermes2) seamlessly.

Add this to your FastAPI/Starlette app or Flask blueprint.
"""

from typing import Optional, Dict, Any
import asyncio
import logging
import hmac
import os
from functools import lru_cache

logger = logging.getLogger(__name__)


# P1-001: SECURITY — Enable Authentication on Remote API
@lru_cache(maxsize=1)
def get_expected_key() -> str:
    """Get expected API key from env (cached)."""
    return os.getenv("HERMES_HTTP_KEY", "")


def verify_api_key(x_hermes_key: Optional[str]) -> bool:
    """Verify API key using constant-time comparison.
    
    P1-001: Implement verify_api_key() with hmac.compare_digest()
    """
    expected = get_expected_key()
    if not expected:
        logger.warning("HERMES_HTTP_KEY not set — API is unauthenticated!")
        return False
    
    if not x_hermes_key:
        return False
    
    # Use constant-time comparison to prevent timing attacks
    return hmac.compare_digest(x_hermes_key, expected)


async def create_remote_api_blueprint(app, gateway_runner):
    """Register remote execution endpoints on the Hermes app.

    This should be called during gateway initialization.

    Args:
        app: FastAPI/Flask application
        gateway_runner: GatewayRunner instance with agent access
    """

    # FastAPI example (add to your main app or create a router)
    try:
        from fastapi import FastAPI, HTTPException, Depends, Header
        from fastapi.responses import JSONResponse

        @app.post("/api/agent/execute")
        async def execute_agent_prompt(
            request: Dict[str, Any],
            x_hermes_key: Optional[str] = Header(None),
            x_hermes_user: Optional[str] = Header(None),
        ):
            """Execute a prompt on this Hermes instance.

            Remote instances call this endpoint to run prompts here.
            Example:
                POST /api/agent/execute
                {
                    "prompt": "What is AI?",
                    "session_id": "telegram_user_123"
                }
            """
            # P1-001: Verify API key
            if not verify_api_key(x_hermes_key):
                logger.warning(f"Unauthorized request from {x_hermes_user or 'unknown'}")
                raise HTTPException(status_code=403, detail="Invalid API key")

            prompt = request.get("prompt", "").strip()
            session_id = request.get("session_id", "remote-exec")

            if not prompt:
                raise HTTPException(status_code=400, detail="prompt required")

            try:
                logger.info(f"[RemoteAPI] Executing prompt from {x_hermes_user} (session: {session_id})")

                # P1-005: Wire actual agent execution
                response = await asyncio.to_thread(
                    gateway_runner.agent.chat,
                    prompt,
                    # TODO: Restore session context if session_id exists
                )

                return {
                    "success": True,
                    "response": response,
                    "session_id": session_id,
                }

            except Exception as e:
                logger.error(f"[RemoteAPI] Execution failed: {e}", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": str(e),
                        "session_id": session_id,
                    },
                )

        @app.get("/health")
        async def health_check():
            """Simple health check endpoint."""
            return {
                "status": "ok",
                "instance": "hermes2",  # TODO: Use config
                "timestamp": None,  # TODO: Use datetime.now()
            }

        @app.get("/api/agent/status")
        async def agent_status(
            x_hermes_key: Optional[str] = Header(None),
            x_hermes_user: Optional[str] = Header(None),
        ):
            """Get current agent status."""
            # TODO: Check if agent is busy, session info, etc.
            return {
                "running": False,
                "current_session": None,
                "model": "claude-3-sonnet",
                "instance": "hermes2",
            }

        logger.info("[RemoteAPI] Registered endpoints: /api/agent/execute, /health, /api/agent/status")

    except ImportError:
        # If not using FastAPI, provide Flask/generic WSGI example
        logger.warning("[RemoteAPI] FastAPI not available; skipping remote API registration")


# Alternative: Generic WSGI/Flask blueprint example
def create_remote_api_flask_blueprint():
    """Flask blueprint for remote execution (alternative to FastAPI)."""
    try:
        from flask import Blueprint, request, jsonify

        api_bp = Blueprint("remote_agent", __name__, url_prefix="/api/agent")

        @api_bp.route("/execute", methods=["POST"])
        def execute_prompt():
            """Execute prompt on this instance."""
            data = request.get_json() or {}
            prompt = data.get("prompt", "").strip()

            if not prompt:
                return jsonify({"error": "prompt required"}), 400

            # TODO: Call gateway_runner.agent.chat(prompt)
            return jsonify({
                "success": True,
                "response": "Not implemented yet",
                "session_id": data.get("session_id"),
            })

        return api_bp

    except ImportError:
        return None
