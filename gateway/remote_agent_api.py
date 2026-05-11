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
from datetime import datetime

logger = logging.getLogger(__name__)


# P1-001: SECURITY — Enable Authentication on Remote API
@lru_cache(maxsize=1)
def get_expected_key() -> str:
    """Get expected API key from env (cached)."""
    return os.getenv("HERMES_REMOTE_API_KEY", "")


def verify_api_key(x_hermes_key: Optional[str]) -> bool:
    """Verify API key using constant-time comparison.
    
    P1-001: Implement verify_api_key() with hmac.compare_digest()
    """
    expected = get_expected_key()
    if not expected:
        logger.warning("HERMES_REMOTE_API_KEY not set — API is unauthenticated!")
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
        from pydantic import BaseModel, Field, field_validator
        from pydantic_core import PydanticCustomError

        # P2-001: Enhanced Pydantic BaseModel with comprehensive validation
        class ExecuteRequest(BaseModel):
            """Request model for /api/agent/execute endpoint.
            
            P2-001: Add Pydantic BaseModel with validation for:
            - agent_id: required string, non-empty
            - prompt: required string, non-empty, max 100KB
            - session_id: optional string
            """
            agent_id: str = Field(
                ...,
                min_length=1,
                max_length=255,
                description="Unique identifier for the agent/instance to execute on"
            )
            prompt: str = Field(
                ...,
                min_length=1,
                max_length=100000,
                description="Prompt text to execute (max 100KB)"
            )
            session_id: Optional[str] = Field(
                default=None,
                max_length=255,
                description="Optional session identifier for tracking"
            )
            
            @field_validator('agent_id', mode='before')
            @classmethod
            def validate_agent_id(cls, v):
                """Validate agent_id: must be non-empty string."""
                if isinstance(v, str):
                    v = v.strip()
                    if not v:
                        raise ValueError('agent_id cannot be empty or whitespace')
                return v
            
            @field_validator('prompt', mode='before')
            @classmethod
            def validate_prompt(cls, v):
                """Validate prompt: must be non-empty string, max 100KB."""
                if isinstance(v, str):
                    v = v.strip()
                    if not v:
                        raise ValueError('prompt cannot be empty or whitespace')
                    # Check length in bytes for strict 100KB limit
                    if len(v.encode('utf-8')) > 100000:
                        raise ValueError(
                            'prompt exceeds maximum length of 100000 bytes'
                        )
                return v
            
            @field_validator('session_id', mode='before')
            @classmethod
            def validate_session_id(cls, v):
                """Validate session_id: optional, but if provided must be non-empty."""
                if v is not None and isinstance(v, str):
                    v = v.strip() or None  # Convert empty string to None
                return v
            
            model_config = {
                "json_schema_extra": {
                    "example": {
                        "agent_id": "default",
                        "prompt": "What is AI?",
                        "session_id": "telegram_user_123"
                    }
                }
            }

        class ExecuteResponse(BaseModel):
            status: str  # "success" or "error"
            output: Optional[str] = None
            error: Optional[str] = None
            session_id: Optional[str] = None
            timestamp: str = ""
        
        class ValidationError(BaseModel):
            """Structure for validation error responses."""
            detail: str
            errors: Optional[list] = None

        @app.post("/api/agent/execute", response_model=ExecuteResponse)
        async def execute_agent_prompt(
            request: ExecuteRequest,
            x_hermes_key: Optional[str] = Header(None),
            x_hermes_user: Optional[str] = Header(None),
        ) -> ExecuteResponse:
            """Execute a prompt on this Hermes instance via InstanceOrchestrator.

            Remote instances call this endpoint to run prompts here.
            Example:
                POST /api/agent/execute
                X-Hermes-Key: <api_key>
                X-Hermes-User: <username>
                
                {
                    "agent_id": "default",
                    "prompt": "What is AI?",
                    "session_id": "telegram_user_123"
                }
                
            Returns:
                {
                    "status": "success" | "error",
                    "output": "response text",
                    "error": null | "error message",
                    "session_id": "telegram_user_123",
                    "timestamp": "2024-05-11T04:08:00Z"
                }
            
            Raises:
                HTTPException(400): Validation error (via Pydantic)
                HTTPException(401): Authentication failure
            """
            # P1-001: Verify API key
            if not verify_api_key(x_hermes_key):
                logger.warning(f"Unauthorized request from {x_hermes_user or 'unknown'}: invalid API key")
                raise HTTPException(status_code=401, detail="Unauthorized")

            # P2-001: Request validation is now handled by Pydantic
            # If validation fails, Pydantic automatically returns 422 with error details
            # We provide custom handling below for better error messages
            
            prompt = request.prompt
            agent_id = request.agent_id
            session_id = request.session_id or "remote-exec"

            try:
                logger.info(
                    f"[RemoteAPI] Executing prompt from {x_hermes_user} "
                    f"(agent_id: {agent_id}, session: {session_id}, len: {len(prompt)})"
                )

                # P1-005: Wire actual agent execution via InstanceOrchestrator
                # The InstanceOrchestrator.execute_on_instance() method handles:
                # - Local vs remote instance determination
                # - HTTP client management
                # - Retry logic with exponential backoff
                # - Health checks and error handling
                
                # Get the orchestrator from gateway_runner
                orchestrator = getattr(gateway_runner, 'instance_orchestrator', None)
                
                if not orchestrator:
                    logger.error("[RemoteAPI] InstanceOrchestrator not available in gateway_runner")
                    raise RuntimeError("Agent orchestrator not configured")

                # Execute the prompt using the orchestrator
                # For local execution, agent_id maps to instance name
                # P1-005: Call execute_on_instance with proper error handling
                response = await orchestrator.execute_on_instance(
                    instance_name=agent_id,  # agent_id is the instance to execute on
                    prompt=prompt,
                    session_id=session_id,
                    max_retries=1,
                )

                # Handle execution failures
                if response is None:
                    # Local instance returns None; use local agent directly
                    logger.info("[RemoteAPI] Local execution requested, using local agent")
                    response = await asyncio.to_thread(
                        gateway_runner.agent.chat,
                        prompt,
                    )

                if isinstance(response, str) and response.startswith("❌"):
                    # Error from orchestrator (e.g., instance not found)
                    logger.error(f"[RemoteAPI] Execution failed: {response}")
                    return ExecuteResponse(
                        status="error",
                        error=response,
                        session_id=session_id,
                        timestamp=datetime.utcnow().isoformat() + "Z",
                    )

                # Success
                return ExecuteResponse(
                    status="success",
                    output=response if isinstance(response, str) else str(response),
                    session_id=session_id,
                    timestamp=datetime.utcnow().isoformat() + "Z",
                )

            except Exception as e:
                logger.error(f"[RemoteAPI] Execution failed: {e}", exc_info=True)
                return ExecuteResponse(
                    status="error",
                    error=str(e)[:500],  # Truncate error message to prevent response bloat
                    session_id=session_id,
                    timestamp=datetime.utcnow().isoformat() + "Z",
                )

        @app.get("/health")
        async def health_check():
            """Simple health check endpoint."""
            return {
                "status": "ok",
                "instance": "hermes",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

        @app.get("/api/agent/status")
        async def agent_status(
            x_hermes_key: Optional[str] = Header(None),
            x_hermes_user: Optional[str] = Header(None),
        ):
            """Get current agent status.
            
            Requires authentication (P1-001).
            """
            # P1-001: Verify API key
            if not verify_api_key(x_hermes_key):
                logger.warning(f"Unauthorized status request from {x_hermes_user or 'unknown'}")
                raise HTTPException(status_code=401, detail="Unauthorized")
            
            # TODO: Check if agent is busy, session info, etc.
            return {
                "running": False,
                "current_session": None,
                "model": "claude-3-sonnet",
                "instance": "hermes",
                "timestamp": datetime.utcnow().isoformat() + "Z",
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
            # P1-001: Verify API key from headers
            api_key = request.headers.get("X-Hermes-Key")
            username = request.headers.get("X-Hermes-User", "unknown")
            
            if not verify_api_key(api_key):
                logger.warning(f"Unauthorized Flask request from {username}: invalid API key")
                return jsonify({"status": "error", "error": "Unauthorized"}), 401

            # P1-005: Parse and validate request
            data = request.get_json() or {}
            prompt = data.get("prompt", "").strip()
            agent_id = data.get("agent_id", "").strip()
            session_id = data.get("session_id") or "remote-exec"

            # Validation errors
            if not agent_id:
                logger.warning(f"Invalid Flask request from {username}: missing agent_id")
                return jsonify({"status": "error", "error": "agent_id is required"}), 400

            if not prompt:
                logger.warning(f"Invalid Flask request from {username}: empty prompt")
                return jsonify({"status": "error", "error": "prompt is required and cannot be empty"}), 400

            MAX_PROMPT_LENGTH = 100000
            if len(prompt) > MAX_PROMPT_LENGTH:
                logger.warning(f"Flask request from {username}: prompt exceeds max length")
                return jsonify({
                    "status": "error",
                    "error": f"prompt exceeds maximum length of {MAX_PROMPT_LENGTH}"
                }), 400

            # TODO: Call orchestrator.execute_on_instance(agent_id, prompt, session_id)
            return jsonify({
                "status": "success",
                "output": "Flask not fully implemented yet",
                "session_id": session_id,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            })

        @api_bp.route("/status", methods=["GET"])
        def agent_status():
            """Get agent status."""
            api_key = request.headers.get("X-Hermes-Key")
            username = request.headers.get("X-Hermes-User", "unknown")
            
            if not verify_api_key(api_key):
                logger.warning(f"Unauthorized Flask status request from {username}")
                return jsonify({"status": "error", "error": "Unauthorized"}), 401

            return jsonify({
                "running": False,
                "current_session": None,
                "model": "claude-3-sonnet",
                "instance": "hermes",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            })

        return api_bp

    except ImportError:
        return None
