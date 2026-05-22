"""
Phase 1.3: OKREngine - Parse OKR with Cognitive Context

Integrates with Nexus Knowledge Base for cognitive understanding.
"""

from okr_executive.engines.base import (
    ExecutionEngine,
    ExecutionContext,
    ExecutionResult,
    EventType,
)
from okr_executive.domain.models import (
    OKR,
    OKRParsedEvent,
)
from typing import Dict, Any, Optional
from datetime import datetime
import json


class OKREngine(ExecutionEngine):
    """
    Parse and understand OKRs using cognitive system.
    
    INTEGRATION:
    - Calls Nexus Knowledge Base for context understanding
    - Extracts org structure, constraints, dependencies
    - Validates OKR structure with cognitive system
    """
    
    async def execute(self, context: ExecutionContext) -> ExecutionContext:
        """
        Execute OKR parsing.
        
        INPUT:
            context.input_data = {
                'okr_text': str,  # Raw OKR input
                'org_context': Dict,  # Org hierarchy info
            }
        
        OUTPUT:
            context.output_data = {
                'okr': OKR dict,
                'cognitive_context': Dict,
                'parsed_at': datetime,
            }
        """
        
        try:
            # Extract input
            okr_text = context.input_data.get('okr_text', '')
            org_context = context.input_data.get('org_context', {})
            
            self.logger.info(f"[{self.engine_id}] Parsing OKR: {okr_text[:50]}...")
            
            # Step 1: Get cognitive context from Nexus
            cognitive_context = await self._get_cognitive_context(okr_text)
            
            # Step 2: Parse OKR structure
            okr = await self._parse_okr_structure(okr_text, cognitive_context)
            
            # Step 3: Validate with cognitive system
            validation = await self._validate_okr(okr, cognitive_context)
            
            if not validation['valid']:
                context.errors.append(f"OKR validation failed: {validation['reason']}")
                return context
            
            # Step 4: Set org context
            okr['org_context'] = org_context
            
            # Step 5: Store output
            context.output_data = {
                'okr': okr,
                'cognitive_context': cognitive_context,
                'validation': validation,
                'parsed_at': datetime.now().isoformat(),
            }
            
            # Step 6: Emit event
            event_payload = {
                'okr_id': okr['id'],
                'objective': okr['objective'],
                'key_results': okr['key_results'],
                'org_context': org_context,
            }
            
            await self.emit_event(
                EventType.OKR_PARSED,
                event_payload,
                context.correlation_id
            )
            
            self.logger.info(f"[{self.engine_id}] ✓ OKR parsed successfully: {okr['id']}")
            
            return context
            
        except Exception as e:
            context.errors.append(str(e))
            self.logger.error(f"[{self.engine_id}] Error: {e}")
            return context
    
    async def _get_cognitive_context(self, okr_text: str) -> Dict[str, Any]:
        """
        Get cognitive understanding from Nexus Knowledge Base.
        
        INTEGRATION POINT: Calls Nexus MCP tools
        """
        
        try:
            # Call Nexus for context
            nexus = self.dependencies.get('nexus')
            if not nexus:
                self.logger.warning("Nexus not available, using basic context")
                return {'source': 'basic', 'scope': 'unknown'}
            
            # Use Nexus NaturalLanguageSearch
            context = await nexus.research({
                'topic': okr_text,
                'depth': 2,
                'focus': 'goal_structure'
            })
            
            return {
                'source': 'nexus',
                'context': context,
                'scope': self._extract_scope(context),
            }
            
        except Exception as e:
            self.logger.warning(f"Could not get Nexus context: {e}")
            return {'source': 'basic', 'scope': 'unknown'}
    
    async def _parse_okr_structure(self, 
                                  okr_text: str,
                                  cognitive_context: Dict) -> Dict[str, Any]:
        """
        Parse OKR into structured format.
        
        Extracts:
        - Objective (main goal)
        - Key Results (measurable outcomes)
        - Priority signals
        """
        
        # For Phase 1, simple parsing
        # Phase 2 will add ML-based parsing
        
        lines = okr_text.strip().split('\n')
        objective = None
        key_results = []
        
        for line in lines:
            line = line.strip()
            if line.startswith('Objective:') or line.startswith('O:'):
                objective = line.replace('Objective:', '').replace('O:', '').strip()
            elif line.startswith('Key Result') or line.startswith('KR'):
                kr = line.replace('Key Result', '').replace('KR', '').strip()
                if kr.startswith(':'):
                    kr = kr[1:].strip()
                if kr:
                    key_results.append(kr)
        
        if not objective:
            objective = lines[0] if lines else "Unnamed OKR"
        
        if not key_results and len(lines) > 1:
            key_results = [line for line in lines[1:] if line.strip()]
        
        okr = OKR(
            objective=objective,
            key_results=key_results,
        )
        
        return okr.dict()
    
    async def _validate_okr(self, 
                           okr: Dict[str, Any],
                           cognitive_context: Dict) -> Dict[str, Any]:
        """
        Validate OKR structure with cognitive system.
        
        INTEGRATION POINT: Calls Nexus validation
        """
        
        issues = []
        
        # Check basic structure
        if not okr.get('objective'):
            issues.append("No objective specified")
        
        if not okr.get('key_results') or len(okr['key_results']) == 0:
            issues.append("No key results specified")
        
        # Try Nexus validation
        try:
            nexus = self.dependencies.get('nexus')
            if nexus:
                validation = await nexus.validate({
                    'component': 'okr_structure',
                    'okr': okr,
                })
                if not validation.get('valid'):
                    issues.extend(validation.get('issues', []))
        except Exception as e:
            self.logger.warning(f"Nexus validation skipped: {e}")
        
        return {
            'valid': len(issues) == 0,
            'issues': issues,
            'reason': '; '.join(issues) if issues else 'Valid',
        }
    
    def _extract_scope(self, context: Dict) -> str:
        """Extract org scope from cognitive context"""
        # Phase 2 enhancement
        return context.get('scope', 'unknown')


# ==================== EXPORTS ====================

__all__ = ['OKREngine']
