"""Cognitive heartbeat control loop — predict-then-declare gate.

Prevents unratified claims from being emitted as complete responses.

When a response contains claim keywords (DONE, COMPLETE, VERIFIED, WORKING, FIXED, SHIPPED),
the heartbeat inspects:
  1. Was a cognitive_predict() tool called in this turn?
  2. If yes, do its falsifier + substrate + budget fields fully specify the claim?
  3. If no, downgrade confidence and surface the gap.

This is a pre-emit gate that runs in turn_finalizer before the result dict is returned.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Keywords that trigger heartbeat inspection
_CLAIM_KEYWORDS = {
    "DONE",
    "COMPLETE",
    "VERIFIED",
    "WORKING",
    "FIXED",
    "SHIPPED",
    "SUCCESS",
    "PASSED",
    "GREEN",
}

# Substring patterns (case-insensitive) that match claim keywords in context
_CLAIM_PATTERNS = [
    r"\b(done|complete|completed|verified|working|fixed|shipped|success|passed|green)\b",
]

_CLAIM_PATTERN_RE = re.compile(
    "|".join(_CLAIM_PATTERNS),
    re.IGNORECASE,
)


def _detect_claim_keywords(text: str) -> bool:
    """Check if text contains any claim keywords."""
    if not text:
        return False
    return bool(_CLAIM_PATTERN_RE.search(text))


def _extract_cognitive_predict_calls(messages: list[dict]) -> list[dict]:
    """Extract any cognitive_predict tool calls from the message history.
    
    Returns list of dicts: {prediction_id, prediction, falsifier, substrate, budget}
    """
    predictions = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tool_call in msg.get("tool_calls", []):
                if tool_call.get("function", {}).get("name") == "cognitive_predict":
                    try:
                        args = json.loads(tool_call.get("function", {}).get("arguments", "{}"))
                        predictions.append({
                            "prediction": args.get("prediction", ""),
                            "falsifier": args.get("falsifier", ""),
                            "substrate": args.get("substrate", ""),
                            "budget": args.get("budget", 3),
                        })
                    except Exception:
                        pass
    return predictions


def perform_cognitive_heartbeat_inspection(
    final_response: str,
    messages: list[dict],
) -> Optional[Dict[str, Any]]:
    """Perform pre-emit heartbeat inspection on the response.
    
    Returns:
      None if response is valid (claim fully specified or no claim detected)
      Dict with warnings if response contains unratified claims:
        {
            "has_claim": bool,
            "claim_found_in": str (location in response where keyword matched),
            "predictions_found": int,
            "prediction_details": str (description of missing fields),
            "confidence_downgrade": float (how much to reduce confidence),
            "soft_warning": str (user-facing message),
        }
    """
    if not final_response:
        return None
    
    # Check if response contains claim keywords
    if not _detect_claim_keywords(final_response):
        return None
    
    # Extract any cognitive_predict calls from this turn
    predictions = _extract_cognitive_predict_calls(messages)
    
    # If no predictions were made but response contains claims → downgrade confidence
    if not predictions:
        # Find where the keyword appears for context
        match = _CLAIM_PATTERN_RE.search(final_response)
        claim_context = (
            final_response[max(0, match.start() - 30):match.end() + 30]
            if match
            else final_response[:100]
        ).replace("\n", " ")
        
        return {
            "has_claim": True,
            "claim_found_in": claim_context,
            "predictions_found": 0,
            "prediction_details": "No cognitive_predict() called before claiming completion",
            "confidence_downgrade": 0.5,
            "soft_warning": (
                "⚠️  Claim detected without pre-declared prediction. "
                "Use cognitive_predict(prediction, falsifier, substrate, budget) "
                "before declaring DONE/COMPLETE/VERIFIED. "
                "Confidence downgraded."
            ),
        }
    
    # Predictions exist — validate that all required fields are populated
    gaps = []
    for i, pred in enumerate(predictions):
        if not pred.get("prediction"):
            gaps.append(f"prediction[{i}]: empty prediction text")
        if not pred.get("falsifier"):
            gaps.append(f"prediction[{i}]: empty falsifier (what would disprove this?)")
        if not pred.get("substrate"):
            gaps.append(f"prediction[{i}]: empty substrate (where was this verified?)")
        if (pred.get("budget") or 0) <= 0:
            gaps.append(f"prediction[{i}]: budget expired ({pred.get('budget')})")
    
    if gaps:
        return {
            "has_claim": True,
            "claim_found_in": final_response[:100].replace("\n", " "),
            "predictions_found": len(predictions),
            "prediction_details": "; ".join(gaps),
            "confidence_downgrade": 0.3,
            "soft_warning": (
                f"⚠️  Prediction recorded but incomplete ({len(gaps)} gap(s)): "
                + "; ".join(gaps[:2]) + ("..." if len(gaps) > 2 else "")
            ),
        }
    
    # All predictions are well-formed
    return None


def inject_heartbeat_warning_into_response(
    final_response: str,
    heartbeat_result: Dict[str, Any],
) -> str:
    """Prepend a soft warning to the response if heartbeat detected gaps.
    
    The warning is prefixed to the response so the user sees it immediately,
    but the response itself is not blocked.
    """
    if not heartbeat_result or not heartbeat_result.get("soft_warning"):
        return final_response
    
    warning = heartbeat_result["soft_warning"]
    return f"{warning}\n\n{final_response}"
