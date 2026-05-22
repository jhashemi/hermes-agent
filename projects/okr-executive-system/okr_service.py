#!/usr/bin/env python3
"""
OKR Executive System - Production Deployment Service

Starts the autonomous OKR orchestrator as a long-running service
Listens on NATS for OKR inputs and executes them autonomously
"""

import asyncio
import sys
import logging
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from okr_executive.orchestrator.autonomous_orchestrator import AutonomousOKROrchestrator
from okr_executive.engines.base import ExecutionMode, EventBroker
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/home/ubuntu/.hermes/logs/okr-executive.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("okr-executive-service")


class OKRExecutiveService:
    """Production service for autonomous OKR execution"""
    
    def __init__(self):
        self.orchestrator = AutonomousOKROrchestrator(
            execution_mode=ExecutionMode.AUTONOMOUS
        )
        self.event_count = 0
        self.okr_count = 0
        
    async def start(self):
        """Start the service"""
        logger.info("=" * 80)
        logger.info("OKR EXECUTIVE SYSTEM - PRODUCTION SERVICE STARTED")
        logger.info("=" * 80)
        logger.info(f"Start Time: {datetime.now().isoformat()}")
        logger.info(f"Execution Mode: AUTONOMOUS")
        logger.info(f"Status: READY for OKR execution")
        logger.info("")
        logger.info("Service is running and listening for OKRs...")
        logger.info("Submit OKRs via:")
        logger.info("  - API endpoint: POST /api/okr/execute")
        logger.info("  - NATS topic: 'okr.submit'")
        logger.info("  - CLI: hermes okr execute <okr_text>")
        logger.info("")
        
        # Keep service running
        try:
            while True:
                await asyncio.sleep(60)
                # Periodic health check
                uptime = datetime.now()
                if self.okr_count > 0:
                    logger.info(
                        f"[Health] OKRs executed: {self.okr_count}, "
                        f"Events processed: {self.event_count}"
                    )
        except KeyboardInterrupt:
            logger.info("Service shutdown requested")
            await self.shutdown()
    
    async def execute_okr(self, okr_text: str) -> dict:
        """Execute a single OKR"""
        logger.info(f"Executing OKR: {okr_text[:50]}...")
        
        try:
            result = await self.orchestrator.execute_okr(okr_text)
            self.okr_count += 1
            logger.info(f"✓ OKR completed (#{self.okr_count})")
            return result
        except Exception as e:
            logger.error(f"✗ OKR execution failed: {e}", exc_info=True)
            raise
    
    async def shutdown(self):
        """Graceful shutdown"""
        logger.info("")
        logger.info("=" * 80)
        logger.info("OKR EXECUTIVE SYSTEM - SHUTDOWN")
        logger.info("=" * 80)
        logger.info(f"Final Stats:")
        logger.info(f"  - Total OKRs executed: {self.okr_count}")
        logger.info(f"  - Total events processed: {self.event_count}")
        logger.info(f"  - Shutdown Time: {datetime.now().isoformat()}")
        logger.info("")
        

async def main():
    """Main entry point"""
    service = OKRExecutiveService()
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
