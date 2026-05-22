"""Remote agent execution API endpoint for multi-instance Hermes.

Each Hermes instance exposes an HTTP API that allows remote instances to:
  POST /api/agent/execute       - Run a prompt and get response
  GET /health                   - Health check
  GET /api/agent/status         - Current agent status

This allows the WhatsApp gateway (44.198.134.0) to dispatch requests to
the agent execution layer (hermes2) seamlessly.

Add this to your FastAPI/Starlette app or Flask blueprint.

SECURITY FIXES:
  - HMAC-SHA256 request signature verification (X-Hermes-Signature)
  - Pydantic input validation (agent_id format, prompt max 10000 chars, session_id format)
  - Shared aiohttp ClientSession with proper lifecycle management
"""

from typing import Optional, Dict, Any
import asyncio
import hashlib
import json
import logging
import hmac
import os
import re
import time
import threading
import warnings
from functools import lru_cache
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================================
# P3-SESSION-LEAK: Shared aiohttp ClientSession manager
# ============================================================================

class SessionManager:
    """Singleton manager for aiohttp ClientSession.

    Ensures a single ClientSession is created once and reused across all
    requests, with proper cleanup on shutdown and detection of unclosed sessions.

    Previously, each outbound call could create a new session, causing file
    descriptor leaks and ResourceWarning on shutdown.
    """

    def __init__(self):
        self._session: Optional[Any] = None  # aiohttp.ClientSession
        self._lock = asyncio.Lock() if asyncio.get_event_loop().is_running() else None
        self._creating = False

    async def get_session(self) -> Any:
        """Get or create the shared aiohttp ClientSession.

        Returns the singleton session, creating it lazily on first access.
        Thread-safe via asyncio.Lock.
        """
        if self._session is not None and not self._session.closed:
            return self._session

        try:
            import aiohttp
        except ImportError:
            raise RuntimeError("aiohttp is required for SessionManager but not installed")

        async with self._get_lock():
            # Double-check after acquiring lock
            if self._session is not None and not self._session.closed:
                return self._session

            logger.info("[SessionManager] Creating shared aiohttp ClientSession")
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                connector=aiohttp.TCPConnector(limit=100, limit_per_host=20),
            )
            return self._session

    def _get_lock(self) -> asyncio.Lock:
        """Get or create the asyncio lock (lazy init for event loop compatibility)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def close_session(self) -> None:
        """Gracefully close the shared session.

        Should be called during application shutdown to prevent
        'Unclosed client session' ResourceWarning.
        """
        if self._session is not None:
            if not self._session.closed:
                logger.info("[SessionManager] Closing shared aiohttp ClientSession")
                await self._session.close()
            self._session = None

    def check_unclosed(self) -> bool:
        """Check if there is an unclosed session (for diagnostics).

        Returns:
            True if there's an unclosed session, False if clean.
        """
        if self._session is not None and not self._session.closed:
            warnings.warn(
                "[SessionManager] Unclosed aiohttp ClientSession detected! "
                "Call close_session() during shutdown to prevent resource leaks.",
                ResourceWarning,
                stacklevel=2,
            )
            return True
        return False

    @property
    def is_initialized(self) -> bool:
        """Check if the session has been initialized."""
        return self._session is not None


# Module-level singleton
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Get or create the global SessionManager instance."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager


async def close_session_manager() -> None:
    """Close the global session manager — call during app shutdown."""
    global _session_manager
    if _session_manager is not None:
        await _session_manager.close_session()
        _session_manager = None


# ============================================================================
# P3-005: RATE LIMITING — Per-API-key rate limiter
# ============================================================================

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
    """Get or create the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(max_requests=100, window_seconds=60)
    return _rate_limiter


# ============================================================================
# P1-001: SECURITY — API Key Authentication
# ============================================================================

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


# ============================================================================
# P1-HMAC: SECURITY — HMAC-SHA256 Request Signature Verification
# ============================================================================

def compute_hmac_signature(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature of the request body.

    Args:
        body: Raw request body bytes
        secret: Secret key (HERMES_REMOTE_API_KEY)

    Returns:
        Hex-encoded HMAC-SHA256 digest string
    """
    return hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def verify_request_signature(body: bytes, signature: Optional[str]) -> bool:
    """Verify HMAC-SHA256 signature of the request body.

    Every request must include an X-Hermes-Signature header containing
    the HMAC-SHA256 of the request body, computed with HERMES_REMOTE_API_KEY.

    Args:
        body: Raw request body bytes
        signature: The X-Hermes-Signature header value (hex-encoded HMAC)

    Returns:
        True if signature is valid, False otherwise
    """
    secret = get_expected_key()
    if not secret:
        logger.warning("HERMES_REMOTE_API_KEY not set — cannot verify request signature!")
        return False

    if not signature:
        logger.warning("Missing X-Hermes-Signature header")
        return False

    # Compute expected signature
    expected_sig = compute_hmac_signature(body, secret)

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(signature, expected_sig)


# ============================================================================
# P2-VALIDATION: Pydantic Input Validation Models
# ============================================================================

# Regex patterns for input format validation
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,254}$")
SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,255}$")
MAX_PROMPT_LENGTH = 10000


def validate_execute_request(data: Dict[str, Any]) -> "ExecuteRequest":
    """Validate and parse an execute request using Pydantic.

    Args:
        data: Raw request data dict (from JSON parsing)

    Returns:
        Validated ExecuteRequest model

    Raises:
        ValueError: If validation fails
    """
    # Import here to allow module-level functions without FastAPI
    from pydantic import BaseModel, Field, field_validator

    class ExecuteRequest(BaseModel):
        """Request model for /api/agent/execute endpoint.

        Validates:
        - agent_id: required, alphanumeric+dashes, 1-255 chars
        - prompt: required, non-empty, max 10000 chars
        - session_id: optional, alphanumeric+dashes+underscores, max 255 chars
        """
        agent_id: str = Field(
            ...,
            min_length=1,
            max_length=255,
            description="Unique identifier for the agent/instance (alphanumeric and dashes only)"
        )
        prompt: str = Field(
            ...,
            min_length=1,
            max_length=MAX_PROMPT_LENGTH,
            description=f"Prompt text to execute (max {MAX_PROMPT_LENGTH} chars)"
        )
        session_id: Optional[str] = Field(
            default=None,
            max_length=255,
            description="Optional session identifier (alphanumeric, dashes, underscores)"
        )

        @field_validator('agent_id', mode='after')
        @classmethod
        def validate_agent_id_format(cls, v):
            """Validate agent_id: alphanumeric and dashes only, must start with alphanumeric."""
            if isinstance(v, str):
                v = v.strip()
                if not v:
                    raise ValueError('agent_id cannot be empty or whitespace')
                if not AGENT_ID_PATTERN.match(v):
                    raise ValueError(
                        'agent_id must contain only alphanumeric characters and dashes, '
                        'and must start with an alphanumeric character'
                    )
            return v

        @field_validator('prompt', mode='after')
        @classmethod
        def validate_prompt_content(cls, v):
            """Validate prompt: non-empty, max 10000 chars."""
            if isinstance(v, str):
                v_stripped = v.strip()
                if not v_stripped:
                    raise ValueError('prompt cannot be empty or whitespace-only')
                if len(v) > MAX_PROMPT_LENGTH:
                    raise ValueError(
                        f'prompt exceeds maximum length of {MAX_PROMPT_LENGTH} characters '
                        f'(got {len(v)})'
                    )
            return v

        @field_validator('session_id', mode='before')
        @classmethod
        def validate_session_id_format(cls, v):
            """Validate session_id: optional, but if provided must match format."""
            if v is not None and isinstance(v, str):
                v = v.strip()
                if not v:
                    return None  # Convert empty string to None
                if not SESSION_ID_PATTERN.match(v):
                    raise ValueError(
                        'session_id must contain only alphanumeric characters, '
                        'dashes, and underscores'
                    )
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

    return ExecuteRequest(**data)


class ExecuteResponse:
    """Simple response model (not Pydantic, used for constructing responses)."""

    @staticmethod
    def success(output: str, session_id: str, timestamp: str) -> Dict[str, Any]:
        return {
            "status": "success",
            "output": output,
            "error": None,
            "session_id": session_id,
            "timestamp": timestamp,
        }

    @staticmethod
    def error(error_msg: str, session_id: str, timestamp: str) -> Dict[str, Any]:
        return {
            "status": "error",
            "output": None,
            "error": error_msg,
            "session_id": session_id,
            "timestamp": timestamp,
        }


# ============================================================================
# FastAPI Blueprint Registration
# ============================================================================

async def create_remote_api_blueprint(app, gateway_runner):
    """Register remote execution endpoints on the Hermes app.

    This should be called during gateway initialization.

    Args:
        app: FastAPI application
        gateway_runner: GatewayRunner instance with agent access
    """

    # FastAPI example (add to your main app or create a router)
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse

        @app.post("/api/agent/execute")
        async def execute_agent_prompt(
            request: Request,
            x_hermes_key: Optional[str] = None,
            x_hermes_signature: Optional[str] = None,
            x_hermes_user: Optional[str] = None,
        ):
            """Execute a prompt on this Hermes instance via InstanceOrchestrator.

            Remote instances call this endpoint to run prompts here.

            Authentication:
                - X-Hermes-Key: API key for basic auth
                - X-Hermes-Signature: HMAC-SHA256 of request body using HERMES_REMOTE_API_KEY

            Example:
                POST /api/agent/execute
                X-Hermes-Key: <api_key>
                X-Hermes-Signature: <hmac-sha256-hex>
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
                HTTPException(401): Authentication failure
                HTTPException(422): Validation error
                HTTPException(429): Rate limit exceeded
            """
            # Read raw body first for HMAC verification
            body = await request.body()

            # P1-HMAC: Verify HMAC-SHA256 request signature
            # Header names are case-insensitive in Starlette, but we also
            # check with hyphenated format for robustness
            if x_hermes_signature is None:
                x_hermes_signature = request.headers.get("x-hermes-signature")
            if not verify_request_signature(body, x_hermes_signature):
                logger.warning(
                    f"Unauthorized request from {x_hermes_user or 'unknown'}: "
                    f"invalid or missing HMAC signature"
                )
                raise HTTPException(
                    status_code=401,
                    detail="Unauthorized: invalid or missing request signature"
                )

            # P1-001: Verify API key
            if x_hermes_key is None:
                x_hermes_key = request.headers.get("x-hermes-key")
            if not verify_api_key(x_hermes_key):
                logger.warning(f"Unauthorized request from {x_hermes_user or 'unknown'}: invalid API key")
                raise HTTPException(status_code=401, detail="Unauthorized")

            # P3-005: Check rate limit
            rate_limiter = get_rate_limiter()
            allowed, retry_after = rate_limiter.is_allowed(x_hermes_key)

            if not allowed:
                logger.warning(
                    f"[RateLimit] Request denied for key: {x_hermes_key[:8]}... "
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

            # P2-VALIDATION: Parse and validate request body with Pydantic
            try:
                parsed = json.loads(body)
            except (json.JSONDecodeError, ValueError) as e:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid JSON: {str(e)}"
                )

            try:
                validated = validate_execute_request(parsed)
            except Exception as e:
                # Pydantic ValidationError or ValueError from our validators
                error_msg = str(e)
                # Extract cleaner error messages from Pydantic
                if hasattr(e, 'errors'):
                    error_details = []
                    for err in e.errors():
                        loc = " -> ".join(str(l) for l in err.get("loc", []))
                        msg = err.get("msg", str(err))
                        error_details.append(f"{loc}: {msg}" if loc else msg)
                    error_msg = "; ".join(error_details)
                raise HTTPException(
                    status_code=422,
                    detail=error_msg,
                )

            prompt = validated.prompt
            agent_id = validated.agent_id
            session_id = validated.session_id or "remote-exec"

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
                    return ExecuteResponse.error(
                        error_msg=response,
                        session_id=session_id,
                        timestamp=datetime.utcnow().isoformat() + "Z",
                    )

                # Success
                return ExecuteResponse.success(
                    output=response if isinstance(response, str) else str(response),
                    session_id=session_id,
                    timestamp=datetime.utcnow().isoformat() + "Z",
                )

            except Exception as e:
                logger.error(f"[RemoteAPI] Execution failed: {e}", exc_info=True)
                return ExecuteResponse.error(
                    error_msg=str(e)[:500],  # Truncate to prevent response bloat
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
            request: Request,
            x_hermes_key: Optional[str] = None,
            x_hermes_signature: Optional[str] = None,
            x_hermes_user: Optional[str] = None,
        ):
            """Get current agent status.

            Requires authentication (P1-001) and HMAC signature.
            """
            # Read raw body for HMAC verification (GET requests have empty body)
            body = await request.body()

            # P1-HMAC: Verify HMAC-SHA256 request signature
            if x_hermes_signature is None:
                x_hermes_signature = request.headers.get("x-hermes-signature")
            if not verify_request_signature(body, x_hermes_signature):
                logger.warning(
                    f"Unauthorized status request from {x_hermes_user or 'unknown'}: "
                    f"invalid or missing HMAC signature"
                )
                raise HTTPException(
                    status_code=401,
                    detail="Unauthorized: invalid or missing request signature"
                )

            # P1-001: Verify API key
            if x_hermes_key is None:
                x_hermes_key = request.headers.get("x-hermes-key")
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

        # P3-SESSION-LEAK: Register startup/shutdown hooks for session lifecycle
        @app.on_event("startup")
        async def _startup_session_manager():
            """Initialize session manager on app startup."""
            get_session_manager()
            logger.info("[RemoteAPI] Session manager initialized")

        @app.on_event("shutdown")
        async def _shutdown_session_manager():
            """Close shared session and rate limiter on shutdown."""
            await close_session_manager()
            get_rate_limiter().shutdown()
            logger.info("[RemoteAPI] Session manager and rate limiter shut down")

        logger.info("[RemoteAPI] Registered endpoints: /api/agent/execute, /health, /api/agent/status")

    except ImportError:
        # If not using FastAPI, provide Flask/generic WSGI example
        logger.warning("[RemoteAPI] FastAPI not available; skipping remote API registration")


# ============================================================================
# Alternative: Generic WSGI/Flask blueprint example
# ============================================================================

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

            # P1-HMAC: Verify HMAC signature
            signature = request.headers.get("X-Hermes-Signature")
            if not verify_request_signature(request.get_data(), signature):
                logger.warning(f"Unauthorized Flask request from {username}: invalid HMAC signature")
                return jsonify({"status": "error", "error": "Unauthorized: invalid signature"}), 401

            # P3-005: Check rate limit
            rate_limiter = get_rate_limiter()
            allowed, retry_after = rate_limiter.is_allowed(api_key)

            if not allowed:
                logger.warning(
                    f"[RateLimit] Flask request denied for key: {api_key[:8]}... "
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

            # P2-VALIDATION: Parse and validate with Pydantic
            data = request.get_json() or {}
            try:
                validated = validate_execute_request(data)
            except Exception as e:
                error_msg = str(e)
                if hasattr(e, 'errors'):
                    error_details = []
                    for err in e.errors():
                        loc = " -> ".join(str(l) for l in err.get("loc", []))
                        msg = err.get("msg", str(err))
                        error_details.append(f"{loc}: {msg}" if loc else msg)
                    error_msg = "; ".join(error_details)
                return jsonify({"status": "error", "error": error_msg}), 422

            prompt = validated.prompt
            agent_id = validated.agent_id
            session_id = validated.session_id or "remote-exec"

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

            # P1-HMAC: Verify HMAC signature on GET requests too
            signature = request.headers.get("X-Hermes-Signature")
            if not verify_request_signature(request.get_data(), signature):
                return jsonify({"status": "error", "error": "Unauthorized: invalid signature"}), 401

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
