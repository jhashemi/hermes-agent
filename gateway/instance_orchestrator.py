"""Multi-instance Hermes orchestrator for WhatsApp gateway.

Allows WhatsApp users to control multiple Hermes instances via slash commands:
  /switch-hermes <instance>    - Switch which instance is controlling responses
  /hermes-list                  - List available instances
  /hermes-status               - Show current active instance

The local WhatsApp gateway (44.198.134.0) receives messages and can either:
1. Execute locally (default)
2. Proxy to a remote instance (hermes2.tailscale or others)

Remote instances are accessed via:
  - SSH tunnel via Tailscale
  - HTTP API on the remote agent
  - Same authentication (Putty HTTP key + username)

This is transparent to the WhatsApp user.
"""

from typing import Optional, Dict, Any
import asyncio
import httpx
import logging
import hashlib
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# P1-002: SECURITY — Fix Input Validation
MAX_CHAT_ID_LENGTH = 256


def validate_hostname(hostname: str) -> bool:
    """Validate that hostname is either a valid IP address or FQDN.
    
    Args:
        hostname: The hostname to validate (IP or FQDN)
    
    Returns:
        True if valid, False otherwise
    
    Raises:
        ValueError: If hostname is not a string or is empty
    """
    if not isinstance(hostname, str):
        raise ValueError(f"hostname must be a string, not {type(hostname).__name__}")
    
    if not hostname or len(hostname.strip()) == 0:
        raise ValueError("hostname cannot be empty")
    
    hostname = hostname.strip()
    
    # Check for valid IPv4 address (exactly 4 octets)
    ipv4_pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
    if re.match(ipv4_pattern, hostname):
        # Validate each octet is 0-255
        parts = hostname.split('.')
        if len(parts) == 4:  # Ensure exactly 4 parts
            try:
                for part in parts:
                    num = int(part)
                    if num < 0 or num > 255:
                        return False
                return True
            except ValueError:
                return False
        return False
    
    # Check for valid IPv6 address (simplified check for :: notation and hex digits)
    if ':' in hostname:
        # Basic IPv6 validation
        try:
            # Check if it looks like IPv6 (contains colons and hex digits)
            if all(c in '0123456789abcdefABCDEF:.[]' for c in hostname):
                return True
        except Exception:
            pass
    
    # Check for valid FQDN (localhost or hostname with letters)
    # FQDN: labels separated by dots, each label 1-63 chars, alphanumeric and hyphens
    # To avoid accepting partial IPs like "192.168.1", we require at least one letter
    if hostname == 'localhost':
        return True
    
    # Pattern for FQDN: must contain at least one letter (not all digits)
    # and follow DNS naming rules
    fqdn_pattern = r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$'
    
    if re.match(fqdn_pattern, hostname):
        # Additional check: must contain at least one letter to distinguish from partial IPs
        if any(c.isalpha() for c in hostname):
            return True
    
    return False


def validate_port(port: int) -> bool:
    """Validate that port is in valid range (1-65535).
    
    Args:
        port: The port number to validate
    
    Returns:
        True if valid, False otherwise
    
    Raises:
        ValueError: If port is not an integer
    """
    if not isinstance(port, int):
        raise ValueError(f"port must be an integer, not {type(port).__name__}")
    
    return 1 <= port <= 65535


class RemoteHermesInstance:
    """Represents a remote Hermes instance available for execution."""

    def __init__(
        self,
        name: str,
        hostname: str,  # e.g., "hermes2.flounder-snake.ts.net"
        ip: str,  # e.g., "100.79.15.66"
        http_port: int = 8000,  # Default Hermes agent HTTP port
        http_key: str = "",  # Shared Putty HTTP key
        username: str = "",  # SSH/HTTP username
        description: str = "",
        is_local: bool = False,
    ):
        self.name = name
        self.hostname = hostname
        self.ip = ip
        self.http_port = http_port
        self.http_key = http_key
        self.username = username
        self.description = description
        self.is_local = is_local

    def get_base_url(self) -> str:
        """Get base URL for this instance (prefer IP via Tailscale)."""
        if self.is_local:
            return "http://127.0.0.1:8000"
        # Use IP address to avoid DNS lookups
        return f"http://{self.ip}:{self.http_port}"

    def get_api_headers(self) -> Dict[str, str]:
        """Get HTTP headers for authentication."""
        headers = {"Content-Type": "application/json"}
        if self.http_key:
            headers["X-Hermes-Key"] = self.http_key
        if self.username:
            headers["X-Hermes-User"] = self.username
        return headers

    def __repr__(self) -> str:
        status = "🟢 LOCAL" if self.is_local else "🔵 REMOTE"
        return f"[{status}] {self.name:20} ({self.hostname}) — {self.description}"


# Registry of available instances
HERMES_INSTANCES: Dict[str, RemoteHermesInstance] = {
    "local": RemoteHermesInstance(
        name="local",
        hostname="127.0.0.1",
        ip="127.0.0.1",
        http_port=8000,
        description="Local Hermes instance (WhatsApp gateway)",
        is_local=True,
    ),
    "hermes2": RemoteHermesInstance(
        name="hermes2",
        hostname="hermes2.flounder-snake.ts.net",
        ip="100.79.15.66",
        http_port=8000,
        http_key="putty_key_here",  # TODO: Load from env
        username="ubuntu",  # TODO: Load from env
        description="Agent execution layer (voice twins + personas)",
        is_local=False,
    ),
    # Add more instances as needed
    # "hermes3": RemoteHermesInstance(...),
}


class InstanceOrchestrator:
    """Manages switching between multiple Hermes instances.

    Maintains session state: which instance is currently handling requests.
    """

    def __init__(self):
        self.current_instance: str = "local"  # Default: WhatsApp gateway's local instance
        self.session_instances: Dict[str, str] = {}  # chat_id → instance_name
        self._http_client: Optional[httpx.AsyncClient] = None
        # P1-004: Health check cache
        self._health_cache: Dict[str, tuple[bool, Any]] = {}  # (healthy, timestamp)
        self._health_cache_ttl = 30  # seconds

    async def init(self):
        """Initialize HTTP client for remote calls."""
        if not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=60)

    async def close(self):
        """Cleanup HTTP client."""
        if self._http_client:
            await self._http_client.aclose()

    def set_current_instance(self, instance_name: str, chat_id: Optional[str] = None) -> bool:
        """Switch to a different instance.

        Args:
            instance_name: Name of instance to switch to
            chat_id: Optional chat ID for per-user instance tracking

        Returns:
            True if switch successful, False if instance not found

        Raises:
            ValueError: If chat_id exceeds MAX_CHAT_ID_LENGTH (prevents DoS via memory exhaustion)
                       or if instance hostname/port are invalid
        """
        if instance_name not in HERMES_INSTANCES:
            return False

        # P2-003: Validate instance hostname and port
        instance = HERMES_INSTANCES[instance_name]
        if not validate_hostname(instance.hostname):
            raise ValueError(f"Invalid hostname for instance '{instance_name}': {instance.hostname} is not a valid IP or FQDN")
        if not validate_port(instance.http_port):
            raise ValueError(f"Invalid port for instance '{instance_name}': {instance.http_port} must be between 1 and 65535")

        if chat_id:
            # P1-002: Validate chat_id length to prevent DoS via unbounded memory allocation
            if not isinstance(chat_id, str):
                raise ValueError(f"chat_id must be a string, not {type(chat_id).__name__}")
            if len(chat_id) > MAX_CHAT_ID_LENGTH:
                logger.warning(f"Chat ID validation failed: length {len(chat_id)} exceeds maximum {MAX_CHAT_ID_LENGTH}")
                raise ValueError(f"chat_id length {len(chat_id)} exceeds maximum {MAX_CHAT_ID_LENGTH}")
            
            # P1-002: Use hash to prevent unbounded growth in session_instances dict
            chat_key = hashlib.sha256(chat_id.encode()).hexdigest()[:32]
            self.session_instances[chat_key] = instance_name
        else:
            self.current_instance = instance_name

        logger.info(f"Switched to instance: {instance_name}")
        return True

    def get_current_instance(self, chat_id: Optional[str] = None) -> str:
        """Get the active instance for a chat."""
        if chat_id:
            # P1-002: Use hash for lookup (consistent with set_current_instance)
            chat_key = hashlib.sha256(chat_id.encode()).hexdigest()[:32]
            if chat_key in self.session_instances:
                return self.session_instances[chat_key]
        return self.current_instance

    def get_instance(self, instance_name: str) -> Optional[RemoteHermesInstance]:
        """Get instance by name."""
        return HERMES_INSTANCES.get(instance_name)

    def list_instances(self) -> str:
        """Format list of available instances."""
        lines = ["🌐 **Available Hermes Instances:**\n"]
        for key, inst in HERMES_INSTANCES.items():
            marker = "→" if key == self.current_instance else " "
            lines.append(f"  {marker} /switch-{key.lower():15} {inst}")
        return "\n".join(lines)

    async def execute_on_instance(
        self,
        instance_name: str,
        prompt: str,
        session_id: str = "",
        max_retries: int = 1,
    ) -> Optional[str]:
        """Execute a prompt on a specific instance.

        Args:
            instance_name: Which instance to run on
            prompt: User prompt/message
            session_id: Hermes session ID (for context)
            max_retries: Number of retry attempts for transient failures

        Returns:
            Agent response, or None if execution failed
        
        Raises:
            ValueError: If instance hostname/port are invalid
        """
        instance = self.get_instance(instance_name)
        if not instance:
            return f"❌ Instance '{instance_name}' not found"

        # P2-003: Validate instance hostname and port before attempting connection
        if not validate_hostname(instance.hostname):
            raise ValueError(f"Invalid hostname for instance '{instance_name}': {instance.hostname} is not a valid IP or FQDN")
        if not validate_port(instance.http_port):
            raise ValueError(f"Invalid port for instance '{instance_name}': {instance.http_port} must be between 1 and 65535")

        # Local execution: return placeholder (gateway handles this)
        if instance.is_local:
            return None  # Let normal gateway handler take over

        # P1-003: Remote execution with proper error handling and retry logic
        for attempt in range(max_retries):
            resp = None
            try:
                if not self._http_client:
                    await self.init()

                url = f"{instance.get_base_url()}/api/agent/execute"
                headers = instance.get_api_headers()
                payload = {
                    "prompt": prompt,
                    "session_id": session_id,
                }

                resp = await self._http_client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=httpx.Timeout(60.0)
                )

                if resp.status == 200:
                    data = resp.json()
                    return data.get("response")
                elif resp.status == 401:
                    # P1-003: Auth failure — reset client to force re-auth on next call
                    logger.error(f"Auth failed for {instance_name}: invalid key")
                    if self._http_client:
                        await self._http_client.aclose()
                        self._http_client = None
                    return f"⚠️ Authentication failed for {instance_name}"
                elif resp.status >= 500:
                    # Server error — retry with exponential backoff
                    logger.warning(f"Server error {resp.status}, retrying... (attempt {attempt+1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1 * (2 ** attempt))  # exponential backoff
                        continue
                    return f"⚠️ Instance '{instance_name}' server error: {resp.status}"
                else:
                    error_text = resp.text if hasattr(resp, 'text') else await resp.text()
                    logger.error(f"Unexpected response {resp.status}: {error_text}")
                    return f"⚠️ Instance '{instance_name}' error: {resp.status}"

            except asyncio.TimeoutError:
                # P1-003: Handle timeout with retry — close on failure to prevent pool exhaustion
                logger.warning(f"Timeout on attempt {attempt+1}/{max_retries}")
                if self._http_client:
                    await self._http_client.aclose()
                    self._http_client = None
                if attempt < max_retries - 1:
                    logger.warning(f"Retrying after timeout...")
                    await asyncio.sleep(1 * (2 ** attempt))
                    continue
                return f"⏱️ Instance '{instance_name}' timed out (>60s)"

            except Exception as e:
                # P1-003: Handle other exceptions with retry — close on failure to prevent pool exhaustion
                logger.warning(f"Execution failed on attempt {attempt+1}/{max_retries}: {e}")
                if self._http_client:
                    await self._http_client.aclose()
                    self._http_client = None
                if attempt < max_retries - 1:
                    logger.warning(f"Retrying after failure...")
                    await asyncio.sleep(1 * (2 ** attempt))
                    continue
                logger.error(f"Failed to execute on {instance_name}: {e}", exc_info=True)
                return f"❌ Could not reach instance '{instance_name}': {str(e)[:100]}"

            finally:
                # P1-003: Ensure response is fully read/consumed to prevent connection pool leak
                # This prevents "connection pool exhaustion" by ensuring httpx properly closes
                # connections even if we didn't explicitly read the response body
                if resp is not None:
                    try:
                        # Consume response body to release connection back to pool
                        _ = resp.content if hasattr(resp, 'content') else resp.read()
                    except Exception as e:
                        logger.debug(f"Error consuming response body: {e}")

        return None

    async def health_check(self, instance_name: str) -> bool:
        """Check if a remote instance is healthy.
        
        P1-004: Add health check caching (30s TTL) and log at ERROR level on failure.
        All failures are logged at ERROR (not DEBUG) for visibility to users.
        """
        instance = self.get_instance(instance_name)
        if not instance or instance.is_local:
            return True  # Local is always healthy

        # P1-004: Check cache first
        if instance_name in self._health_cache:
            healthy, timestamp = self._health_cache[instance_name]
            if datetime.now() - timestamp < timedelta(seconds=self._health_cache_ttl):
                return healthy

        try:
            if not self._http_client:
                await self.init()

            url = f"{instance.get_base_url()}/health"
            headers = instance.get_api_headers()

            resp = await self._http_client.get(url, headers=headers, timeout=5)
            healthy = resp.status == 200
            self._health_cache[instance_name] = (healthy, datetime.now())

            if not healthy:
                # P1-004: Log at ERROR level so failures are VISIBLE (not hidden in DEBUG)
                logger.error(f"❌ HEALTH CHECK FAILED: {instance_name} returned HTTP {resp.status} (expected 200)")

            return healthy
        except asyncio.TimeoutError:
            # P1-004: Handle timeout explicitly — log at ERROR for visibility
            logger.error(f"❌ HEALTH CHECK TIMEOUT: {instance_name} did not respond within 5 seconds")
            self._health_cache[instance_name] = (False, datetime.now())
            return False
        except Exception as e:
            # P1-004: Log at ERROR level with full traceback for debugging
            logger.error(f"❌ HEALTH CHECK ERROR: {instance_name} — {e}", exc_info=True)
            self._health_cache[instance_name] = (False, datetime.now())
            return False

    async def get_instance_status(self, instance_name: str) -> Dict[str, Any]:
        """Get detailed status of a specific instance.
        
        P1-004: Returns structured status including health check results.
        Ensures failures are visible to callers (not hidden in debug logs).
        
        Returns:
            Dict with keys:
            - name: instance name
            - healthy: bool
            - status_message: human-readable status
            - reachable: bool (True if instance is reachable)
            - error: error message if not reachable
        """
        instance = self.get_instance(instance_name)
        
        if not instance:
            return {
                "name": instance_name,
                "healthy": False,
                "status_message": f"❌ Instance '{instance_name}' not found",
                "reachable": False,
                "error": "Instance does not exist in registry"
            }
        
        if instance.is_local:
            return {
                "name": instance_name,
                "healthy": True,
                "status_message": "🟢 LOCAL instance (always healthy)",
                "reachable": True,
                "error": None
            }
        
        # Check remote instance health
        try:
            healthy = await self.health_check(instance_name)
            
            if healthy:
                return {
                    "name": instance_name,
                    "healthy": True,
                    "status_message": f"🔵 {instance_name} is HEALTHY and reachable",
                    "reachable": True,
                    "error": None
                }
            else:
                error_msg = f"⚠️ {instance_name} health check FAILED — instance may be unreachable"
                # P1-004: Log failure at ERROR level for visibility
                logger.error(f"HEALTH CHECK FAILED: {instance_name} is not responding to health check")
                
                # Notify if hermes2 specifically is unreachable
                if instance_name == "hermes2":
                    logger.error(f"🚨 CRITICAL: Remote instance 'hermes2' is unreachable! Users cannot access remote execution.")
                
                return {
                    "name": instance_name,
                    "healthy": False,
                    "status_message": error_msg,
                    "reachable": False,
                    "error": "Health check failed"
                }
        
        except Exception as e:
            error_msg = f"❌ Failed to check status of {instance_name}: {str(e)}"
            # P1-004: Log at ERROR level so failures are visible (not DEBUG)
            logger.error(f"Exception during health check for {instance_name}: {e}", exc_info=True)
            
            # Notify if hermes2 specifically failed
            if instance_name == "hermes2":
                logger.error(f"🚨 CRITICAL: Cannot reach remote instance 'hermes2': {e}")
            
            return {
                "name": instance_name,
                "healthy": False,
                "status_message": error_msg,
                "reachable": False,
                "error": str(e)
            }

    async def get_status(self, chat_id: Optional[str] = None) -> str:
        """Get status of current instance."""
        current = self.get_current_instance(chat_id)
        
        # P1-004: Use new get_instance_status for consistent, user-visible output
        status_dict = await self.get_instance_status(current)
        
        if status_dict["healthy"]:
            return (
                f"{status_dict['status_message']}\\n"
                f"Hostname: {self.get_instance(current).hostname}\\n"
                f"\\n"
                f"Available instances: /hermes-list"
            )
        else:
            # P1-004: Make failures prominent to users
            return (
                f"{status_dict['status_message']}\\n"
                f"Error: {status_dict['error']}\\n"
                f"\\n"
                f"Try switching instances: /hermes-list"
            )
