#!/usr/bin/env python3
"""Simple test tool handler for skill tool discovery tests."""

import json


def handle_echo_message(args: dict, **kwargs) -> str:
    """Echo back the provided message.
    
    Args:
        args: Dict with 'message' key
    
    Returns:
        JSON with echoed message
    """
    message = args.get("message", "")
    return json.dumps({
        "success": True,
        "echo": message,
    })


def handle_add_numbers(args: dict, **kwargs) -> str:
    """Add two numbers.
    
    Args:
        args: Dict with 'a' and 'b' keys
    
    Returns:
        JSON with result
    """
    try:
        a = float(args.get("a", 0))
        b = float(args.get("b", 0))
        return json.dumps({
            "success": True,
            "result": a + b,
        })
    except (ValueError, TypeError) as e:
        return json.dumps({
            "error": str(e),
        })
