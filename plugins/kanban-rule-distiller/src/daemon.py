#!/usr/bin/env python3
"""Kanban Rule Distiller daemon entry point.

Runs as:
  python -m plugins.kanban_rule_distiller.daemon

Or via systemd:
  systemctl --user start kanban-rule-distiller.service
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# Add parent to path so we can import hermes modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.distiller import RuleDistiller


def setup_logging() -> None:
    """Configure logging for the service."""
    log_level = os.getenv("HERMES_RULE_DISTILLER_LOG_LEVEL", "INFO").upper()
    
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
    """Main entry point."""
    setup_logging()
    logger = logging.getLogger(__name__)
    
    logger.info("Kanban Rule Distiller starting")
    
    try:
        # Load config from environment
        poll_interval = float(os.getenv("HERMES_RULE_DISTILLER_POLL_INTERVAL", "5.0"))
        model = os.getenv("HERMES_RULE_DISTILLER_MODEL", "haiku-4-5")
        
        # Create distiller instance
        distiller = RuleDistiller(
            model=model,
            poll_interval=poll_interval,
        )
        
        # Run the service
        asyncio.run(distiller.run())
        return 0
    except Exception as e:
        logger.error(f"Daemon error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
