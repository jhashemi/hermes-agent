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
import ipaddress
import logging
import hashlib
import re
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# P1-002: SECURITY — Fix Input Validation
MAX_CHAT_ID_LENGTH = 256


# P2-005: Runtime environment variable loading
def get_instance_config() -> Dict[str, Any]:
    """Load instance configuration from environment at runtime.
    
    P2-005: This allows dynamic config changes without restart.
    Re-reads os.environ on each call to enable live configuration updates.
    
    Returns:
        Dict with keys:
        - remote_api_key: API key for remote instance auth (no default)
        - instance_a_hostname: Hostname for instance A (default: 'localhost')
        - instance_a_port: Port for instance A (default: 8000)
    
    Raises:
        ValueError: If environment variables have invalid values
    """
    config = {}
    
    # Load HERMES_REMOTE_API_KEY (optional, no default)
    config['remote_api_key'] = os.environ.get('HERMES_REMOTE_API_KEY', '').strip()
    if not config['remote_api_key']:
        logger.debug('HERMES_REMOTE_API_KEY not set in environment')
    
    # Load HERMES_INSTANCE_A_HOSTNAME (optional, default: 'localhost')
    config['instance_a_hostname'] = os.environ.get(
        'HERMES_INSTANCE_A_HOSTNAME', 
        'localhost'
    ).strip()
    if not config['instance_a_hostname']:
        config['instance_a_hostname'] = 'localhost'
        logger.warning('HERMES_INSTANCE_A_HOSTNAME is empty, using default: localhost')
    
    # Load HERMES_INSTANCE_A_PORT (optional, default: 8000)
    try:
        port_str = os.environ.get('HERMES_INSTANCE_A_PORT', '8000').strip()
        config['instance_a_port'] = int(port_str)
        if not validate_port(config['instance_a_port']):
            raise ValueError(f'Port {config["instance_a_port"]} is out of valid range (1-65535)')
    except ValueError as e:
        logger.warning(f'Invalid HERMES_INSTANCE_A_PORT value: {e}, using default: 8000')
        config['instance_a_port'] = 8000
    
    return config


def validate_hostname(hostname: str) -> bool:
    """Validate that hostname is either a valid IP address or FQDN.

    P2-003 (t_fcc68f00): Uses ipaddress.ip_address() for strict IPv4/IPv6
    validation per DoD requirement #1. Falls back to FQDN regex for
    non-IP hostnames. FQDN rules require at least 2 labels with the final
    label (TLD) starting with a letter, so partial IPs like "192.168.1.a"
    and bare labels like "invalid" are rejected.

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

    # Strict IP validation (IPv4 + IPv6) via stdlib. Handles all edge cases:
    # rejects "999.999.999.999", "192.168.1", "192.168.1.1.1", "192.168.-1.1",
    # and accepts "::1", "2001:db8::1", "fe80::1".
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass

    # Special case: bare "localhost" is a valid hostname even though it's
    # a single label (no TLD). Common in dev/test configs.
    if hostname == "localhost":
        return True

    # FQDN validation: must be at least 2 labels (label.tld), each label
    # 1-63 chars of alphanumerics/hyphens (no leading/trailing hyphen),
    # and the final label (TLD) must start with a letter. The letter-start
    # TLD rule is what rejects partial IPs like "192.168.1.a" (TLD "a" ok)
    # vs. "192.168.1.5" (TLD "5" — rejected).
    #
    # We split on "." and validate each label individually rather than one
    # mega-regex — clearer and easier to reason about.
    if "." not in hostname:
        # Single-label hostname other than "localhost" is not a valid FQDN.
        return False

    labels = hostname.split(".")
    if len(labels) < 2:
        return False

    label_pattern = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
    for i, label in enumerate(labels):
        if not label:
            # Empty label means consecutive dots or leading/trailing dot.
            return False
        if not label_pattern.match(label):
            return False

    # TLD (final label) must start with a letter. This is the guard that
    # rejects partial IPs like "192.168.1.5" and ambiguous strings like
    # "1.2.3.4.5" (which ipaddress already rejected but might slip through
    # if fewer octets), while still accepting "x.co", "example.com", etc.
    tld = labels[-1]
    if not tld[0].isalpha():
        return False

    # Second-to-last label (SLD, the "domain" component) must contain at
    # least one letter. This rejects partial-IP shapes like "192.168.1.a"
    # (SLD "1" — all digits) while accepting real FQDNs whose SLDs are
    # always word-like ("example", "flounder-snake", "x", "example-456").
    sld = labels[-2]
    if not any(c.isalpha() for c in sld):
        return False

    return True


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
        ip: str,  # e.g., "hermes2.flounder-snake.ts.net"
        http_port: int = 8000,  # Default Hermes agent HTTP port
        http_key: str = "",  # Shared Putty HTTP key
        username: str = "",  # SSH/HTTP username
        description: str = "",
        is_local: bool = False,
    ):
        # P2-003 (t_fcc68f00) DoD #4: validate at construction time so bad
        # configs blow up early rather than at first use. Both `hostname` and
        # `ip` can hold either an IP literal or a DNS name (see the
        # "hermes2" registry entry that uses the FQDN for both), so both
        # are checked against the tolerant validate_hostname().
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Invalid instance name for {name!r}: must be non-empty string")
        if not validate_hostname(hostname):
            raise ValueError(
                f"Invalid hostname for instance {name!r}: {hostname!r} "
                f"is not a valid IPv4/IPv6 address or FQDN"
            )
        if not validate_hostname(ip):
            raise ValueError(
                f"Invalid ip for instance {name!r}: {ip!r} "
                f"is not a valid IPv4/IPv6 address or FQDN"
            )
        if not validate_port(http_port):
            raise ValueError(
                f"Invalid http_port for instance {name!r}: {http_port!r} "
                f"must be an integer in [1, 65535]"
            )

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


# t_17cfbbf1: Runtime instance registry loading.
#
# Previously HERMES_INSTANCES was a module-level constant built at import time,
# which baked env vars (HERMES2_IP, http_key, username) into the process for its
# lifetime — no reload without a Python restart. Now the registry is built by
# load_instances_from_config(), which reads os.environ on every call. The
# module-level HERMES_INSTANCES is initialized from the same loader (kept as a
# stable import symbol for backward compat with existing tests), and
# InstanceOrchestrator.__init__() calls the loader itself so each orchestrator
# instance sees the *current* env, not the env at module import.
def load_instances_from_config() -> Dict[str, "RemoteHermesInstance"]:
    """Build the instance registry from environment at runtime.

    Reads os.environ on every call — no import-time caching — so callers can
    pick up env changes by re-invoking this function (or by constructing a
    fresh InstanceOrchestrator, which calls it in __init__).

    Environment variables consumed:
      - HERMES2_IP: IP/hostname for the hermes2 instance (default: FQDN)
      - HERMES2_HTTP_KEY / HERMES_HTTP_KEY: shared HTTP auth key for hermes2
        (HERMES2_HTTP_KEY takes precedence; HERMES_HTTP_KEY is a fleet-wide
        fallback consumed by the gateway's auth layer)
      - HERMES2_USERNAME: SSH/HTTP username for hermes2 (default: "ubuntu")
      - HERMES2_PORT: HTTP port for hermes2 (default: 8000)

    Returns:
        Fresh Dict[str, RemoteHermesInstance] reflecting current env state.
    """
    hermes2_ip = os.environ.get("HERMES2_IP", "hermes2.flounder-snake.ts.net")
    # HERMES2_HTTP_KEY is the per-instance override; HERMES_HTTP_KEY is the
    # fleet-wide default (same var the gateway's verify_api_key layer reads).
    hermes2_key = (
        os.environ.get("HERMES2_HTTP_KEY")
        or os.environ.get("HERMES_HTTP_KEY", "")
    )
    hermes2_username = os.environ.get("HERMES2_USERNAME", "ubuntu")
    try:
        hermes2_port = int(os.environ.get("HERMES2_PORT", "8000"))
        if not validate_port(hermes2_port):
            logger.warning(
                f"HERMES2_PORT {hermes2_port} out of range 1-65535, using 8000"
            )
            hermes2_port = 8000
    except ValueError:
        logger.warning(
            f"HERMES2_PORT not an integer ({os.environ.get('HERMES2_PORT')!r}), using 8000"
        )
        hermes2_port = 8000

    return {
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
            ip=hermes2_ip,
            http_port=hermes2_port,
            http_key=hermes2_key,
            username=hermes2_username,
            description="Agent execution layer (voice twins + personas)",
            is_local=False,
        ),
        # Add more instances as needed via env vars or config.yaml
    }


# Backward-compat module-level symbol. Tests and gateway code import this name;
# it is initialised at import from the same runtime loader used by orchestrator
# instances. Per-instance registries in InstanceOrchestrator.__init__ do NOT
# read this — they call load_instances_from_config() directly, so changing env
# vars and constructing a new orchestrator sees the change even if this
# module-level dict is stale.
HERMES_INSTANCES: Dict[str, "RemoteHermesInstance"] = load_instances_from_config()


def reload_hermes_instances() -> Dict[str, "RemoteHermesInstance"]:
    """Rebuild the module-level HERMES_INSTANCES dict from current env.

    Use when callers rely on the module-level symbol (e.g. legacy imports) and
    need it refreshed after env vars change. Existing InstanceOrchestrator
    instances are NOT retroactively updated — call orchestrator.reload_instances()
    on each live instance for that.
    """
    global HERMES_INSTANCES
    HERMES_INSTANCES = load_instances_from_config()
    return HERMES_INSTANCES


class InstanceOrchestrator:
    """Manages switching between multiple Hermes instances.

    Maintains session state: which instance is currently handling requests.
    """

    def __init__(self, instances: Optional[Dict[str, "RemoteHermesInstance"]] = None):
        # t_17cfbbf1: Registry is loaded at __init__ time (runtime), not at
        # module import. Callers can inject a specific registry (tests) or let
        # us pull the current one from env via load_instances_from_config().
        # Changing HERMES_HTTP_KEY / HERMES2_* env vars and constructing a
        # fresh InstanceOrchestrator picks up the new values without a Python
        # restart — that is the whole point of this refactor.
        if instances is not None:
            self._instances: Dict[str, "RemoteHermesInstance"] = dict(instances)
        else:
            self._instances = load_instances_from_config()
        self.current_instance: str = "local"  # Default: WhatsApp gateway's local instance
        self.session_instances: Dict[str, str] = {}  # chat_id → instance_name
        self._http_client: Optional[httpx.AsyncClient] = None
        # P1-004: Health check cache
        self._health_cache: Dict[str, tuple[bool, Any]] = {}  # (healthy, timestamp)
        self._health_cache_ttl = 30  # seconds

    def reload_instances(self) -> Dict[str, "RemoteHermesInstance"]:
        """Rebuild this orchestrator's instance registry from current env.

        Use when env vars (HERMES_HTTP_KEY, HERMES2_*, etc.) have changed and
        this orchestrator instance needs to pick them up without a restart.
        Also refreshes the module-level HERMES_INSTANCES symbol for callers
        that import it directly.
        """
        self._instances = load_instances_from_config()
        # Keep the module-level symbol in sync so legacy consumers see the
        # same registry state.
        global HERMES_INSTANCES
        HERMES_INSTANCES = dict(self._instances)
        # Invalidate health cache — old entries may reference stale hosts/keys.
        self._health_cache.clear()
        return self._instances

    def _get_registry(self) -> Dict[str, "RemoteHermesInstance"]:
        """Return the effective instance registry for lookups.

        Primary source is self._instances (populated by load_instances_from_config
        at __init__ time). We overlay any entries added directly to the
        module-level HERMES_INSTANCES dict AFTER construction so callers that
        register new instances via the legacy module-level surface (existing
        tests, code that predates this refactor) still work. self._instances
        wins on key collision — a per-orchestrator entry is authoritative.
        """
        merged: Dict[str, "RemoteHermesInstance"] = dict(HERMES_INSTANCES)
        merged.update(self._instances)
        return merged

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
        registry = self._get_registry()
        if instance_name not in registry:
            return False

        # P2-003: Validate instance hostname and port
        instance = registry[instance_name]
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
        return self._get_registry().get(instance_name)

    def list_instances(self) -> str:
        """Format list of available instances."""
        lines = ["🌐 **Available Hermes Instances:**\n"]
        for key, inst in self._get_registry().items():
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
        
        P2-005: Loads environment variables at runtime to allow dynamic config changes.
        """
        # P2-005: Load runtime configuration from environment
        config = get_instance_config()
        logger.debug(f"Loaded instance config at runtime: hostname={config['instance_a_hostname']}, port={config['instance_a_port']}")
        
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
        
        P2-005: Loads environment variables at runtime to allow dynamic config changes.
        
        Returns:
            Dict with keys:
            - name: instance name
            - healthy: bool
            - status_message: human-readable status
            - reachable: bool (True if instance is reachable)
            - error: error message if not reachable
        """
        # P2-005: Load runtime configuration from environment
        config = get_instance_config()
        logger.debug(f"Loaded instance config at runtime in get_instance_status: hostname={config['instance_a_hostname']}, port={config['instance_a_port']}")
        
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
                f"{status_dict['status_message']}\\\n"
                f"Hostname: {self.get_instance(current).hostname}\\\n"
                f"\\\n"
                f"Available instances: /hermes-list"
            )
        else:
            # P1-004: Make failures prominent to users
            return (
                f"{status_dict['status_message']}\\\n"
                f"Error: {status_dict['error']}\\\n"
                f"\\\n"
                f"Try switching instances: /hermes-list"
            )
