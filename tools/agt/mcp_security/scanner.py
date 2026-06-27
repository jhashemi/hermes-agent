"""
tools/agt/mcp_security/scanner.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
MCPSecurityScanner — static analysis for MCP tool definitions.

Implements the six threat-detection layers mandated by L4 rule
``mcp-security-gateway-scanner-coverage`` (runtime.l4 §80):

    1. TOOL_POISONING      — schema abuse / hidden required fields
    2. RUG_PULL            — SHA-256 fingerprint drift
    3. CROSS_SERVER_ATTACK — tool-name impersonation across servers
    4. TYPOSQUATTING       — edit-distance ≤ 2 against known tool names
    5. HIDDEN_INSTRUCTION  — invisible unicode / HTML comments
    6. DESCRIPTION_INJECTION — prompt-injection patterns

Stdlib only: re, unicodedata, dataclasses, enum, hashlib, datetime,
difflib, json, typing.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Public enumerations
# ---------------------------------------------------------------------------

VERSION = "0.1.0"


class ThreatClass(str, Enum):
    """Six threat classes mandated by runtime.l4 §required_threat_classes."""

    TOOL_POISONING = "TOOL_POISONING"
    RUG_PULL = "RUG_PULL"
    CROSS_SERVER_ATTACK = "CROSS_SERVER_ATTACK"
    TYPOSQUATTING = "TYPOSQUATTING"
    HIDDEN_INSTRUCTION = "HIDDEN_INSTRUCTION"
    DESCRIPTION_INJECTION = "DESCRIPTION_INJECTION"


class Severity(str, Enum):
    """Severity levels per mcp-security-gateway-scanner-coverage."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------

_EVIDENCE_MAX = 512  # bytes — integration.l4 message-shape rule


@dataclass
class Threat:
    """A single threat finding produced by a detector.

    ``evidence`` is truncated to 512 chars in ``__post_init__`` to satisfy
    the integration.l4 message-shape rule referenced by
    mcp-security-gateway-scanner-coverage.
    """

    threat_class: ThreatClass
    severity: Severity
    tool_name: str
    server_name: str
    evidence: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:  # noqa: D401
        """Enforce the 512-char evidence truncation contract."""
        if isinstance(self.evidence, str) and len(self.evidence) > _EVIDENCE_MAX:
            self.evidence = self.evidence[:_EVIDENCE_MAX]


@dataclass
class ScanResult:
    """Aggregated result for a single tool scan.

    The ``clean`` property returns True when no CRITICAL threats are present,
    matching the rejection predicate in mcp-security-gateway-scanner-coverage
    §98-99.
    """

    tool_name: str
    server_name: str
    threats: List[Threat] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Return True iff no CRITICAL threat was detected."""
        return not any(t.severity == Severity.CRITICAL for t in self.threats)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

#: Suspicious field names that indicate schema abuse (tool poisoning).
_POISONED_FIELDS = frozenset(
    {
        "system_prompt",
        "override_instructions",
        "eval_code",
        "exec",
        "__system__",
        "inject_prompt",
    }
)

#: Invisible / control Unicode code-points that hide instructions.
_HIDDEN_UNICODE_RANGES: List[Tuple[int, int]] = [
    (0x200B, 0x200B),  # zero-width space
    (0x200C, 0x200C),  # zero-width non-joiner
    (0x200D, 0x200D),  # zero-width joiner
    (0x202A, 0x202E),  # bidi override block
    (0x2060, 0x2060),  # word joiner
    (0xFEFF, 0xFEFF),  # BOM / zero-width no-break space
]

#: Compiled regex: HTML / Markdown comments.
_RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL | re.IGNORECASE)

#: Compiled regexes for description injection patterns.
_INJECTION_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I), "ignore previous instructions"),
    (re.compile(r"\bsystem\s*:", re.I), "system: role override"),
    (re.compile(r"<\|im_start\|>", re.I), "<|im_start|> token injection"),
    (re.compile(r"\bact\s+as\s+(a\s+)?", re.I), "act-as role override"),
    (re.compile(r"\bnew\s+instructions\s*:", re.I), "new instructions override"),
    (re.compile(r"\bforget\s+(everything|all)\b", re.I), "forget everything override"),
    # Destructive shell patterns
    (re.compile(r"rm\s+-rf\s+/", re.I), "destructive shell: rm -rf /"),
    (re.compile(r":\(\)\{:\|:&\};:", re.I), "fork-bomb pattern"),
]

#: Levenshtein distance threshold for typosquatting.
_LEV_THRESHOLD = 2
#: SequenceMatcher similarity threshold for typosquatting.
_SEQ_THRESHOLD = 0.85


def _levenshtein(a: str, b: str) -> int:
    """Pure stdlib Levenshtein edit distance."""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (0 if ca == cb else 1)))
        prev = curr
    return prev[-1]


def _fingerprint(description: str, schema: Optional[dict]) -> str:
    """SHA-256 fingerprint of a tool definition (pure)."""
    schema_str = json.dumps(schema, sort_keys=True) if schema is not None else ""
    raw = (description or "") + schema_str
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _has_hidden_unicode(text: str) -> List[str]:
    """Return list of hidden unicode chars found in *text* (pure)."""
    found: List[str] = []
    for ch in text:
        cp = ord(ch)
        for lo, hi in _HIDDEN_UNICODE_RANGES:
            if lo <= cp <= hi:
                found.append(f"U+{cp:04X} ({unicodedata.name(ch, 'UNKNOWN')})")
                break
    return found


# ---------------------------------------------------------------------------
# MCPSecurityScanner
# ---------------------------------------------------------------------------


class MCPSecurityScanner:
    """Static analyser for MCP tool definitions.

    Implements all six detection layers required by L4 rule
    ``mcp-security-gateway-scanner-coverage``.

    The internal registry maps ``(server_name, tool_name)`` to its last
    known SHA-256 fingerprint.  Only ``register_tool`` mutates registry
    state; all six detector methods are pure.
    """

    def __init__(self, registry: Optional[Dict[Tuple[str, str], str]] = None) -> None:
        """Initialise scanner with an optional pre-populated registry.

        Args:
            registry: Mapping of ``(server_name, tool_name)`` to
                      fingerprint hash.  Defaults to an empty dict.
        """
        # {(server_name, tool_name): fingerprint_hash}
        self._registry: Dict[Tuple[str, str], str] = dict(registry) if registry else {}
        self.version: str = VERSION

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_tool(
        self,
        name: str,
        description: str,
        schema: Optional[dict],
        server_name: str,
    ) -> None:
        """Store a canonical fingerprint for a tool definition.

        Subsequent ``scan_tool`` calls for the same ``(server_name, name)``
        pair will check for rug-pull drift against this baseline.

        Idempotent: re-registering with identical content is a no-op.
        """
        fp = _fingerprint(description, schema)
        self._registry[(server_name, name)] = fp

    def scan_tool(
        self,
        tool_name: str,
        description: str,
        schema: Optional[dict] = None,
        server_name: str = "",
    ) -> List[Threat]:
        """Run all six threat detectors against a single tool definition.

        Returns a (possibly empty) list of :class:`Threat` objects.
        This method never mutates registry state — only ``register_tool``
        does (mcp-security-gateway-scanner-coverage §62).

        Args:
            tool_name:   MCP tool name.
            description: Tool description string.
            schema:      JSON-Schema dict (inputSchema), optional.
            server_name: Originating MCP server identifier.
        """
        threats: List[Threat] = []

        threats.extend(self._detect_tool_poisoning(tool_name, description, schema, server_name))
        threats.extend(self._detect_rug_pull(tool_name, description, schema, server_name))
        threats.extend(self._detect_cross_server_attack(tool_name, description, schema, server_name))
        threats.extend(self._detect_typosquatting(tool_name, description, schema, server_name))
        threats.extend(self._detect_hidden_instruction(tool_name, description, schema, server_name))
        threats.extend(self._detect_description_injection(tool_name, description, schema, server_name))

        return threats

    def scan_server(
        self,
        server_name: str,
        tools: List[dict],
    ) -> Dict[str, "ScanResult"]:
        """Batch-scan all tools from a server.

        Args:
            server_name: MCP server identifier.
            tools: List of tool dicts with keys ``name``, ``description``,
                   and optionally ``schema`` / ``inputSchema``.

        Returns:
            Mapping of tool name → :class:`ScanResult`.
        """
        results: Dict[str, ScanResult] = {}
        for tool in tools:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            schema = tool.get("schema") or tool.get("inputSchema")
            threats = self.scan_tool(name, desc, schema, server_name)
            results[name] = ScanResult(tool_name=name, server_name=server_name, threats=threats)
        return results

    # ------------------------------------------------------------------
    # Private detectors — all PURE (no side effects)
    # ------------------------------------------------------------------

    def _detect_tool_poisoning(
        self,
        tool_name: str,
        description: str,
        schema: Optional[dict],
        server_name: str,
    ) -> List[Threat]:
        """Detect schema abuse: hidden required fields / poisoned property names.

        Checks ``schema.required[]`` and ``schema.properties`` keys against
        the known set of suspicious field names mandated by
        mcp-security-gateway-scanner-coverage §63.
        """
        threats: List[Threat] = []
        if not schema or not isinstance(schema, dict):
            return threats

        required_fields = schema.get("required", [])
        properties = schema.get("properties", {})

        # Check required[] for poisoned field names
        for field_name in required_fields:
            if isinstance(field_name, str) and field_name.lower() in _POISONED_FIELDS:
                threats.append(
                    Threat(
                        threat_class=ThreatClass.TOOL_POISONING,
                        severity=Severity.CRITICAL,
                        tool_name=tool_name,
                        server_name=server_name,
                        evidence=(
                            f"Suspicious required field '{field_name}' found in "
                            f"schema.required for tool '{tool_name}'"
                        ),
                    )
                )

        # Check property names for poisoned field names (not already flagged)
        flagged_required = {
            f.lower() for f in required_fields if isinstance(f, str) and f.lower() in _POISONED_FIELDS
        }
        for prop_name in properties:
            if isinstance(prop_name, str) and prop_name.lower() in _POISONED_FIELDS:
                if prop_name.lower() not in flagged_required:
                    threats.append(
                        Threat(
                            threat_class=ThreatClass.TOOL_POISONING,
                            severity=Severity.CRITICAL,
                            tool_name=tool_name,
                            server_name=server_name,
                            evidence=(
                                f"Suspicious property name '{prop_name}' found in "
                                f"schema.properties for tool '{tool_name}'"
                            ),
                        )
                    )

        return threats

    def _detect_rug_pull(
        self,
        tool_name: str,
        description: str,
        schema: Optional[dict],
        server_name: str,
    ) -> List[Threat]:
        """Detect definition drift from the registered fingerprint.

        Only fires when ``(server_name, tool_name)`` has been previously
        registered via ``register_tool``.  A mismatch indicates the tool
        definition was changed after initial admission — a rug pull.

        References mcp-security-gateway-scanner-coverage §64 (RUG_PULL).
        """
        key = (server_name, tool_name)
        if key not in self._registry:
            return []

        current_fp = _fingerprint(description, schema)
        stored_fp = self._registry[key]
        if current_fp == stored_fp:
            return []

        return [
            Threat(
                threat_class=ThreatClass.RUG_PULL,
                severity=Severity.CRITICAL,
                tool_name=tool_name,
                server_name=server_name,
                evidence=(
                    f"Fingerprint mismatch for tool '{tool_name}' on server '{server_name}'. "
                    f"Stored: {stored_fp[:16]}… Current: {current_fp[:16]}…"
                ),
            )
        ]

    def _detect_cross_server_attack(
        self,
        tool_name: str,
        description: str,
        schema: Optional[dict],
        server_name: str,
    ) -> List[Threat]:
        """Detect tool-name impersonation across different servers.

        If the same ``tool_name`` is already registered under a *different*
        ``server_name``, it is likely an impersonation attempt.

        References mcp-security-gateway-scanner-coverage §65 (CROSS_SERVER_ATTACK).
        """
        for (reg_server, reg_name) in self._registry:
            if reg_name == tool_name and reg_server != server_name:
                return [
                    Threat(
                        threat_class=ThreatClass.CROSS_SERVER_ATTACK,
                        severity=Severity.CRITICAL,
                        tool_name=tool_name,
                        server_name=server_name,
                        evidence=(
                            f"Tool '{tool_name}' is already registered from server "
                            f"'{reg_server}' — potential impersonation by '{server_name}'"
                        ),
                    )
                ]
        return []

    def _detect_typosquatting(
        self,
        tool_name: str,
        description: str,
        schema: Optional[dict],
        server_name: str,
    ) -> List[Threat]:
        """Detect typosquatting: close name similarity against other-server tools.

        Uses both difflib.SequenceMatcher ratio ≥ 0.85 and Levenshtein
        edit distance ≤ 2 to catch substitution attacks.

        Severity is WARNING (not CRITICAL) per mcp-security-gateway-scanner-coverage §66.
        """
        threats: List[Threat] = []
        seen_matches: set = set()

        for (reg_server, reg_name) in self._registry:
            if reg_server == server_name:
                # Only flag names from OTHER servers
                continue
            if reg_name == tool_name:
                # Exact match — handled by _detect_cross_server_attack
                continue
            if reg_name in seen_matches:
                continue

            lev = _levenshtein(tool_name, reg_name)
            ratio = difflib.SequenceMatcher(None, tool_name, reg_name).ratio()

            if lev <= _LEV_THRESHOLD or ratio >= _SEQ_THRESHOLD:
                seen_matches.add(reg_name)
                threats.append(
                    Threat(
                        threat_class=ThreatClass.TYPOSQUATTING,
                        severity=Severity.WARNING,
                        tool_name=tool_name,
                        server_name=server_name,
                        evidence=(
                            f"Tool '{tool_name}' (server='{server_name}') closely resembles "
                            f"'{reg_name}' (server='{reg_server}'): "
                            f"lev={lev}, ratio={ratio:.3f}"
                        ),
                    )
                )

        return threats

    def _detect_hidden_instruction(
        self,
        tool_name: str,
        description: str,
        schema: Optional[dict],
        server_name: str,
    ) -> List[Threat]:
        """Detect invisible unicode and HTML/Markdown comments in descriptions.

        Checks for:
        - Zero-width / bidi-override / BOM unicode code points
        - HTML comments ``<!-- ... -->``

        References mcp-security-gateway-scanner-coverage §67 (HIDDEN_INSTRUCTION).
        """
        threats: List[Threat] = []
        text = description or ""

        # 1. Invisible unicode
        hidden_chars = _has_hidden_unicode(text)
        if hidden_chars:
            unique_chars = list(dict.fromkeys(hidden_chars))[:8]  # dedupe, cap list
            threats.append(
                Threat(
                    threat_class=ThreatClass.HIDDEN_INSTRUCTION,
                    severity=Severity.CRITICAL,
                    tool_name=tool_name,
                    server_name=server_name,
                    evidence=(
                        f"Invisible unicode characters detected in description of "
                        f"'{tool_name}': {', '.join(unique_chars)}"
                    ),
                )
            )

        # 2. HTML / Markdown comments
        comments = _RE_HTML_COMMENT.findall(text)
        if comments:
            snippet = comments[0][:120]
            threats.append(
                Threat(
                    threat_class=ThreatClass.HIDDEN_INSTRUCTION,
                    severity=Severity.CRITICAL,
                    tool_name=tool_name,
                    server_name=server_name,
                    evidence=(
                        f"HTML/Markdown comment detected in description of '{tool_name}': "
                        f"{snippet!r}"
                    ),
                )
            )

        return threats

    def _detect_description_injection(
        self,
        tool_name: str,
        description: str,
        schema: Optional[dict],
        server_name: str,
    ) -> List[Threat]:
        """Detect prompt-injection / role-override patterns in descriptions.

        Checks against a curated list of injection regexes including:
        - ``ignore (all )?previous instructions``
        - ``system:`` role override
        - ``<|im_start|>`` token injection
        - ``act as (a )?`` role assignment
        - ``new instructions:`` override
        - ``forget (everything|all)`` wipe instruction
        - Destructive shell patterns (``rm -rf /``, fork bomb)

        References mcp-security-gateway-scanner-coverage §68 (DESCRIPTION_INJECTION).
        """
        threats: List[Threat] = []
        text = description or ""

        for pattern, label in _INJECTION_PATTERNS:
            m = pattern.search(text)
            if m:
                snippet = text[max(0, m.start() - 20) : m.end() + 20]
                threats.append(
                    Threat(
                        threat_class=ThreatClass.DESCRIPTION_INJECTION,
                        severity=Severity.CRITICAL,
                        tool_name=tool_name,
                        server_name=server_name,
                        evidence=(
                            f"Injection pattern '{label}' matched in description of "
                            f"'{tool_name}': …{snippet!r}…"
                        ),
                    )
                )

        return threats
