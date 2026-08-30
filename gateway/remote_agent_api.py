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
import re
import time
import threading
from functools import lru_cache
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# INPUT-INVARIANT-01 (KR-1): Bounded chat_id validation
#
# chat_id is an opaque platform identifier (Telegram numeric, Discord snowflake,
# WhatsApp phone ID, etc.) carried in session_id or agent routing. It must be
# bounded to prevent:
#   - Resource exhaustion (unbounded string → memory DoS in session DB indexes)
#   - Injection (control characters, SQL/NoSQL payloads in routing keys)
#   - Log forging (newlines in chat_id → agent.log manipulation)
#
# Invariant:
#   max_length = 256 (covers Discord snowflake ~20 chars, Telegram ~12, with
#   generous headroom for composite keys like "telegram:user:12345:thread:67890")
#   charset = printable ASCII excluding control chars and shell metacharacters
#   format = non-empty after strip, no newlines, no null bytes
#
# Enforced at 2 layers:
#   1. API layer (ExecuteRequest.session_id validator)
#   2. Queue consumer layer (validate_chat_id called before routing)
# ---------------------------------------------------------------------------

CHAT_ID_MAX_LENGTH = 256
_CHAT_ID_ALLOWED_CHARS = re.compile(
    r"^[a-zA-Z0-9_\-.:/@]+$"
)


def validate_chat_id(chat_id: Optional[str], *, field_name: str = "chat_id") -> Optional[str]:
    """Validate and normalize a chat_id against the documented invariant.

    Returns the stripped, validated chat_id, or None if the input is None.
    Raises ValueError if the input violates any invariant:
      - exceeds CHAT_ID_MAX_LENGTH
      - contains control characters, newlines, null bytes
      - contains characters outside the allowed charset
      - is empty after stripping

    This function is the single source of truth for chat_id validation —
    both the API layer (Pydantic validator) and the queue consumer layer
    call it, ensuring defense in depth.
    """
    if chat_id is None:
        return None
    if not isinstance(chat_id, str):
        raise ValueError(f"{field_name} must be a string")

    stripped = chat_id.strip()
    if not stripped:
        raise ValueError(f"{field_name} cannot be empty or whitespace")

    if len(stripped) > CHAT_ID_MAX_LENGTH:
        raise ValueError(
            f"{field_name} exceeds maximum length of {CHAT_ID_MAX_LENGTH} characters"
        )

    # Reject control characters, null bytes, newlines (log forging / injection)
    if any(ord(c) < 32 or ord(c) == 127 for c in stripped):
        raise ValueError(f"{field_name} contains control characters")

    # Enforce charset: alphanumeric, underscore, hyphen, dot, colon, slash, at
    if not _CHAT_ID_ALLOWED_CHARS.match(stripped):
        raise ValueError(
            f"{field_name} contains disallowed characters "
            f"(allowed: alphanumeric, _-./:@)"
        )

    return stripped


# P3-005: RATE LIMITING — Per-API-key rate limiter
class RateLimiter:
    """Per-API-key rate limiter with automatic counter reset.
    
    Limits: 100 requests per 60 seconds per API key.
    Returns 429 Too Many Requests when exceeded.
    Includes Retry-After header.
    Tracks request counts in memory using a dict.
    Resets counters every 60 seconds.
    
    Thread-safe implementation using a lock.
    """
    
    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        """Initialize the rate limiter.
        
        Args:
            max_requests: Maximum requests per window (default: 100)
            window_seconds: Time window in seconds (default: 60)
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        
        # Track request counts: {api_key: [(timestamp, count), ...]}
        # We keep track of request timestamps for more accurate counting
        self.request_history: Dict[str, list] = {}
        
        # Lock for thread-safe access
        self._lock = threading.RLock()
        
        # Start background cleanup thread
        self._cleanup_thread = None
        self._stop_cleanup = False
        self._start_cleanup_thread()
        
        logger.info(
            f"[RateLimiter] Initialized: {max_requests} requests per {window_seconds} seconds"
        )
    
    def _start_cleanup_thread(self):
        """Start a background thread to clean up expired entries."""
        def cleanup_loop():
            while not self._stop_cleanup:
                time.sleep(self.window_seconds)
                self._cleanup_expired()
        
        self._cleanup_thread = threading.Thread(daemon=True, target=cleanup_loop)
        self._cleanup_thread.start()
    
    def _cleanup_expired(self):
        """Remove request entries older than window_seconds."""
        current_time = time.time()
        cutoff_time = current_time - self.window_seconds
        
        with self._lock:
            # Remove old entries and keys with no requests
            to_delete = []
            for api_key, timestamps in self.request_history.items():
                # Keep only recent timestamps
                self.request_history[api_key] = [
                    ts for ts in timestamps if ts > cutoff_time
                ]
                # Remove key if no requests remain
                if not self.request_history[api_key]:
                    to_delete.append(api_key)
            
            for api_key in to_delete:
                del self.request_history[api_key]
    
    def is_allowed(self, api_key: str) -> tuple[bool, Optional[int]]:
        """Check if a request is allowed for the given API key.
        
        Args:
            api_key: The API key to check
            
        Returns:
            (allowed, retry_after_seconds)
            - allowed: True if request is allowed, False if rate limit exceeded
            - retry_after_seconds: If rate limited, seconds to wait; None otherwise
        """
        current_time = time.time()
        cutoff_time = current_time - self.window_seconds
        
        with self._lock:
            # Initialize if not seen before
            if api_key not in self.request_history:
                self.request_history[api_key] = []
            
            # Clean up old entries for this key
            self.request_history[api_key] = [
                ts for ts in self.request_history[api_key] if ts > cutoff_time
            ]
            
            # Count recent requests
            request_count = len(self.request_history[api_key])
            
            if request_count < self.max_requests:
                # Allow the request and record it
                self.request_history[api_key].append(current_time)
                return (True, None)
            else:
                # Rate limit exceeded
                # Find the oldest request to calculate when next request is allowed
                oldest_timestamp = self.request_history[api_key][0]
                next_allowed_time = oldest_timestamp + self.window_seconds
                retry_after = max(1, int(next_allowed_time - current_time))
                
                logger.warning(
                    f"[RateLimiter] Rate limit exceeded for API key: {api_key[:8]}... "
                    f"({request_count}/{self.max_requests} requests in {self.window_seconds}s)"
                )
                return (False, retry_after)
    
    def get_stats(self, api_key: str) -> Dict[str, Any]:
        """Get current rate limit stats for an API key.
        
        Args:
            api_key: The API key to check
            
        Returns:
            Dictionary with:
            - requests_made: Number of requests in current window
            - requests_remaining: Requests allowed before limit
            - reset_in_seconds: Seconds until window resets
        """
        current_time = time.time()
        cutoff_time = current_time - self.window_seconds
        
        with self._lock:
            if api_key not in self.request_history:
                self.request_history[api_key] = []
            
            # Clean up old entries
            self.request_history[api_key] = [
                ts for ts in self.request_history[api_key] if ts > cutoff_time
            ]
            
            request_count = len(self.request_history[api_key])
            requests_remaining = max(0, self.max_requests - request_count)
            
            if self.request_history[api_key]:
                oldest_timestamp = self.request_history[api_key][0]
                reset_in = max(0, int(oldest_timestamp + self.window_seconds - current_time))
            else:
                reset_in = 0
            
            return {
                "requests_made": request_count,
                "requests_remaining": requests_remaining,
                "reset_in_seconds": reset_in,
                "max_requests": self.max_requests,
                "window_seconds": self.window_seconds,
            }
    
    def shutdown(self):
        """Gracefully shutdown the rate limiter."""
        self._stop_cleanup = True
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=2)
        logger.info("[RateLimiter] Shut down")


# Global rate limiter instance (per-API-key)
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get or create the global rate limiter instance.

    Phase 2 policy (t_b292b559): 10 requests per 60 seconds, keyed by
    X-Hermes-User header. See use sites in execute_agent_prompt (FastAPI)
    and execute_prompt (Flask blueprint).
    """
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(max_requests=10, window_seconds=60)
    return _rate_limiter


def _rate_limit_key(
    x_hermes_user: Optional[str],
    x_hermes_key: Optional[str],
) -> str:
    """Compute the rate-limit bucket key for a request.

    Task t_b292b559 requires keying by X-Hermes-User. When that header is
    absent (e.g. legacy callers that only send X-Hermes-Key), we fall back
    to hashing the API key so unauthenticated hammering still gets bucketed.
    The bucket string is prefixed to keep user- and key-buckets disjoint.
    """
    if x_hermes_user:
        stripped = x_hermes_user.strip()
        if stripped:
            return f"user:{stripped}"
    if x_hermes_key:
        # Prefix so a user literally named "<key>:<value>" cannot collide
        # with a real key bucket. Truncate for log-safe key ids elsewhere.
        return f"key:{x_hermes_key}"
    return "anonymous:"


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
                """Validate session_id (carries chat_id): bounded by INPUT-INVARIANT-01.

                Enforces chat_id invariant at the API layer:
                - max 256 chars
                - no control characters / newlines / null bytes
                - allowed charset: alphanumeric, _-./:@
                - empty → None (optional field)
                """
                if v is not None and isinstance(v, str):
                    v = v.strip() or None  # Convert empty string to None
                    if v is not None:
                        validate_chat_id(v, field_name="session_id")
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
                HTTPException(429): Rate limit exceeded
            """
            # P1-001: Verify API key
            if not verify_api_key(x_hermes_key):
                logger.warning(f"Unauthorized request from {x_hermes_user or 'unknown'}: invalid API key")
                raise HTTPException(status_code=401, detail="Unauthorized")

            # P3-005 / t_b292b559: Check rate limit (10/min per X-Hermes-User)
            rate_limiter = get_rate_limiter()
            rl_key = _rate_limit_key(x_hermes_user, x_hermes_key)
            allowed, retry_after = rate_limiter.is_allowed(rl_key)
            
            if not allowed:
                logger.warning(
                    f"[RateLimit] Request denied for bucket: {rl_key[:32]}... "
                    f"from {x_hermes_user or 'unknown'} (retry after {retry_after}s)"
                )
                response = JSONResponse(
                    status_code=429,
                    content={
                        "status": "error",
                        "error": "Rate limit exceeded",
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    }
                )
                response.headers["Retry-After"] = str(retry_after)
                return response

            # P2-001: Request validation is now handled by Pydantic
            # If validation fails, Pydantic automatically returns 422 with error details
            # We provide custom handling below for better error messages
            
            prompt = request.prompt
            agent_id = request.agent_id
            session_id = request.session_id or "remote-exec"

            # INPUT-INVARIANT-01 (KR-1): Layer-2 enforcement at the queue
            # consumer boundary. Even if the Pydantic validator passes,
            # re-validate session_id (which carries chat_id) here before it
            # reaches the orchestrator routing. Defense in depth: the API
            # layer (Pydantic) is layer 1, this is layer 2.
            try:
                validate_chat_id(session_id, field_name="session_id")
            except ValueError as ve:
                logger.warning(
                    "[RemoteAPI] Rejected session_id at layer-2: %s", ve
                )
                raise HTTPException(
                    status_code=400, detail=str(ve)
                ) from ve

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
        async def health_check(
            x_hermes_key: Optional[str] = Header(None),
        ):
            """Simple health check endpoint.

            AUTH-GATE-ZERO (KR-1): Even health checks must be authenticated on
            remote API surfaces. An unauthenticated /health leaks deployment
            fingerprints (instance name, timestamp, uptime) to scanners and
            is a standard reconnaissance vector. We return 401 if no key is
            configured (fail-closed) or if the key is missing/invalid.

            A minimal unauthenticated liveness probe is intentionally NOT
            provided here — operators should use the dashboard's own
            /api/health (loopback-bound) or a TCP-level check instead.
            """
            if not verify_api_key(x_hermes_key):
                logger.warning("Unauthorized health check from unknown")
                raise HTTPException(status_code=401, detail="Unauthorized")
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

            # P3-005 / t_b292b559: Check rate limit (10/min per X-Hermes-User)
            rate_limiter = get_rate_limiter()
            rl_key = _rate_limit_key(username if username != "unknown" else None, api_key)
            allowed, retry_after = rate_limiter.is_allowed(rl_key)
            
            if not allowed:
                logger.warning(
                    f"[RateLimit] Flask request denied for bucket: {rl_key[:32]}... "
                    f"from {username} (retry after {retry_after}s)"
                )
                response = jsonify({
                    "status": "error",
                    "error": "Rate limit exceeded",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                })
                response.status_code = 429
                response.headers["Retry-After"] = str(retry_after)
                return response

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
