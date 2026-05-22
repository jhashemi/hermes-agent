"""
Phase 1.4: ResearchEngine - Expert Research with Embodied Agents

Integrates with ExecutiveAgentTwin for expert research on OKR.
"""

from okr_executive.engines.base import (
    ExecutionEngine,
    ExecutionContext,
    EventType,
)
from okr_executive.domain.models import ResearchCompletedEvent
from typing import Dict, Any, List
from datetime import datetime


class ResearchEngine(ExecutionEngine):
    """
    Research OKR with embodied executive agent + cognitive system.
    
    INTEGRATION:
    - Calls ExecutiveAgentTwin.research() for expert analysis
    - Uses Nexus Knowledge Base for domain knowledge
    - Generates research findings, approaches, and confidence scores
    """
    
    async def execute(self, context: ExecutionContext) -> ExecutionContext:
        """
        Execute research phase.
        
        INPUT:
            context.input_data = {
                'okr': OKR dict,
                'cognitive_context': Dict,
            }
        
        OUTPUT:
            context.output_data = {
                'research_findings': Dict,
                'recommended_approaches': List,
                'confidence_scores': Dict,
            }
        """
        
        try:
            okr = context.input_data.get('okr', {})
            cognitive_context = context.input_data.get('cognitive_context', {})
            
            okr_id = okr.get('id')
            objective = okr.get('objective', '')
            
            self.logger.info(f"[{self.engine_id}] Researching OKR: {objective[:50]}...")
            
            # Step 1: Call ExecutiveAgentTwin for research
            research_result = await self._call_embodied_agent_research(
                okr_id=okr_id,
                objective=objective,
                key_results=okr.get('key_results', []),
                cognitive_context=cognitive_context
            )
            
            if not research_result['success']:
                context.errors.append(f"Agent research failed: {research_result['error']}")
                return context
            
            # Step 2: Use Nexus for additional context
            nexus_insights = await self._get_nexus_insights(objective)
            
            # Step 3: Synthesize findings
            findings = {
                'agent_research': research_result.get('findings', {}),
                'nexus_insights': nexus_insights,
                'integrated_findings': await self._integrate_findings(
                    research_result.get('findings', {}),
                    nexus_insights
                ),
            }
            
            # Step 4: Generate recommended approaches
            recommended_approaches = await self._generate_approaches(
                okr_id=okr_id,
                research_result=research_result,
                findings=findings
            )
            
            # Step 5: Calculate confidence scores
            confidence_scores = self._calculate_confidence(
                research_result,
                nexus_insights,
                recommended_approaches
            )
            
            # Step 6: Store output
            context.output_data = {
                'research_findings': findings,
                'recommended_approaches': recommended_approaches,
                'confidence_scores': confidence_scores,
            }
            
            # Step 7: Emit event
            event_payload = {
                'okr_id': okr_id,
                'research_findings': findings,
                'recommended_approaches': recommended_approaches,
                'confidence_scores': confidence_scores,
            }
            
            await self.emit_event(
                EventType.RESEARCH_COMPLETED,
                event_payload,
                context.correlation_id
            )
            
            self.logger.info(f"[{self.engine_id}] ✓ Research completed: {len(recommended_approaches)} approaches")
            
            return context
            
        except Exception as e:
            context.errors.append(str(e))
            self.logger.error(f"[{self.engine_id}] Error: {e}")
            return context
    
    async def _call_embodied_agent_research(self,
                                          okr_id: str,
                                          objective: str,
                                          key_results: List[str],
                                          cognitive_context: Dict) -> Dict[str, Any]:
        """
        Call ExecutiveAgentTwin for research.
        
        INTEGRATION POINT: Calls embodied agent framework
        """
        
        try:
            # Get embodied agent
            agent = self.dependencies.get('embodied_agent')
            if not agent:
                return {
                    'success': False,
                    'error': 'Embodied agent not available'
                }
            
            # Call agent research method
            # Pattern from ExecutiveAgentsFramework audit
            research = await agent.research(
                objective=objective,
                key_results=key_results,
                cognitive_context=cognitive_context,
                okr_id=okr_id
            )
            
            return {
                'success': True,
                'findings': research,
                'agent_id': agent.id,
            }
            
        except Exception as e:
            self.logger.error(f"Agent research error: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def _get_nexus_insights(self, objective: str) -> Dict[str, Any]:
        """
        Get insights from Nexus Knowledge Base.
        
        INTEGRATION POINT: Calls Nexus MCP tools
        """
        
        try:
            nexus = self.dependencies.get('nexus')
            if not nexus:
                return {'source': 'unavailable'}
            
            # Use Nexus to research similar patterns
            insights = await nexus.research({
                'topic': objective,
                'depth': 2,
                'focus': 'similar_approaches'
            })
            
            return {
                'source': 'nexus',
                'insights': insights,
            }
            
        except Exception as e:
            self.logger.warning(f"Nexus insights error: {e}")
            return {'source': 'unavailable', 'error': str(e)}
    
    async def _integrate_findings(self,
                                 agent_research: Dict,
                                 nexus_insights: Dict) -> Dict[str, Any]:
        """Integrate agent research with Nexus insights"""
        
        return {
            'agent_methods': agent_research.get('methods', []),
            'similar_patterns': nexus_insights.get('insights', []),
            'risk_factors': agent_research.get('risks', []),
            'success_factors': agent_research.get('success_factors', []),
        }
    
    async def _generate_approaches(self,
                                  okr_id: str,
                                  research_result: Dict,
                                  findings: Dict) -> List[Dict[str, Any]]:
        """Generate recommended approaches"""
        
        approaches = []
        
        # Extract from agent research
        agent_methods = research_result.get('findings', {}).get('methods', [])
        for i, method in enumerate(agent_methods[:5]):  # Top 5
            approaches.append({
                'id': f"approach-{i+1}",
                'name': method.get('name', f'Approach {i+1}'),
                'description': method.get('description', ''),
                'effort': method.get('effort', 'unknown'),
                'timeline_weeks': method.get('timeline_weeks', 0),
                'resource_cost': method.get('cost', 0.0),
                'risks': method.get('risks', []),
                'success_probability': method.get('success_probability', 0.5),
            })
        
        return approaches
    
    def _calculate_confidence(self,
                             research_result: Dict,
                             nexus_insights: Dict,
                             recommended_approaches: List) -> Dict[str, float]:
        """Calculate confidence scores"""
        
        base_confidence = 0.7
        
        # Adjust based on data availability
        if research_result.get('success'):
            base_confidence += 0.1
        
        if nexus_insights.get('source') == 'nexus':
            base_confidence += 0.1
        
        # Per-approach confidence
        approach_confidence = {}
        for approach in recommended_approaches:
            score = approach.get('success_probability', 0.5)
            approach_confidence[approach['id']] = score
        
        return {
            'overall': min(base_confidence, 0.95),
            'approaches': approach_confidence,
            'data_quality': 'high' if len(recommended_approaches) > 0 else 'low',
        }


# ==================== EXPORTS ====================

__all__ = ['ResearchEngine']
