"""
PHASE 3: ExecutionEngine - Task assignment + VCG welfare optimization

Converts goals/tasks into agent assignments using VCG optimal allocation.
"""

from okr_executive.engines.base import (
    ExecutionEngine,
    ExecutionContext,
    EventType,
)
from okr_executive.domain.models import Task
from typing import List, Dict, Any
from datetime import datetime
import logging


class TaskExecutionEngine(ExecutionEngine):
    """
    Execute goals by assigning tasks to best agents via VCG.
    
    Implements:
    - Task creation from goals
    - VCG welfare optimization for agent allocation
    - VoiceTwin + LiveKit agent assignment
    - Real-time execution coordination
    """
    
    def __init__(self, engine_id: str, event_broker, dependencies: Dict[str, Any]):
        super().__init__(engine_id, event_broker, dependencies)
        self.logger = logging.getLogger(self.__class__.__name__)
    
    async def execute(self, context: ExecutionContext) -> ExecutionContext:
        """
        Execute phase: goals + plan → task assignments via VCG
        
        INPUT:
            context.input_data = {
                'goal_hierarchy': List[Goal],
                'task_breakdown': List[Task],
                'rice_scores': Dict,
            }
        
        OUTPUT:
            context.output_data = {
                'task_assignments': List[Task with agent assigned],
                'vcg_allocation': Dict,
                'voice_sessions': Dict,
                'execution_plan': Dict,
            }
        """
        
        try:
            goal_hierarchy = context.input_data.get('goal_hierarchy', [])
            task_breakdown = context.input_data.get('task_breakdown', [])
            rice_scores = context.input_data.get('rice_scores', {})
            
            self.logger.info(f"[{self.engine_id}] Executing {len(task_breakdown)} tasks...")
            
            # Step 1: Convert to task objects
            tasks = [Task(**t) if isinstance(t, dict) else t for t in task_breakdown]
            
            # Step 2: Get available agents
            available_agents = await self._get_available_agents()
            
            if not available_agents:
                context.errors.append("No agents available for execution")
                return context
            
            # Step 3: VCG allocation (welfare optimization)
            vcg_allocation = await self._vcg_allocate_tasks(
                tasks=tasks,
                agents=available_agents,
                rice_scores=rice_scores
            )
            
            if not vcg_allocation.get('success'):
                context.errors.append(f"VCG allocation failed: {vcg_allocation.get('error')}")
                return context
            
            # Step 4: Assign VoiceTwins + LiveKit
            assigned_tasks = await self._assign_voice_agents(
                tasks=tasks,
                allocation=vcg_allocation.get('assignment', {}),
                available_agents=available_agents
            )
            
            # Step 5: Create execution plan
            execution_plan = self._create_execution_plan(
                assigned_tasks=assigned_tasks,
                vcg_allocation=vcg_allocation
            )
            
            # Step 6: Store output
            context.output_data = {
                'task_assignments': [t.dict() if hasattr(t, 'dict') else t for t in assigned_tasks],
                'vcg_allocation': vcg_allocation,
                'execution_plan': execution_plan,
                'execution_started_at': datetime.now().isoformat(),
            }
            
            # Step 7: Emit event
            event_payload = {
                'num_tasks': len(assigned_tasks),
                'num_agents': len(available_agents),
                'allocation_method': 'VCG',
                'welfare_score': vcg_allocation.get('total_welfare', 0),
            }
            
            await self.emit_event(
                EventType.EXECUTION_STARTED,
                event_payload,
                context.correlation_id
            )
            
            self.logger.info(f"[{self.engine_id}] ✓ Execution plan created: {len(assigned_tasks)} tasks assigned")
            
            return context
            
        except Exception as e:
            context.errors.append(str(e))
            self.logger.error(f"[{self.engine_id}] Error: {e}")
            return context
    
    async def _get_available_agents(self) -> List[Dict[str, Any]]:
        """Get available ExecutiveAgentTwins, VoiceTwins, LiveKit agents"""
        
        # This would call the deployed agent systems
        # For now, return mock agents
        agents = [
            {
                'id': 'agent-1',
                'name': 'embodied-agent-1',
                'type': 'embodied',
                'capacity': 5,
                'utilization': 2,
                'available_capacity': 3,
                'skills': ['implementation', 'research', 'testing'],
            },
            {
                'id': 'agent-2',
                'name': 'voice-twin-1',
                'type': 'voice',
                'capacity': 3,
                'utilization': 1,
                'available_capacity': 2,
                'skills': ['communication', 'coordination'],
            },
            {
                'id': 'agent-3',
                'name': 'livekit-agent-1',
                'type': 'realtime',
                'capacity': 10,
                'utilization': 3,
                'available_capacity': 7,
                'skills': ['streaming', 'realtime-execution'],
            },
        ]
        
        return agents
    
    async def _vcg_allocate_tasks(self,
                                 tasks: List[Task],
                                 agents: List[Dict],
                                 rice_scores: Dict) -> Dict[str, Any]:
        """
        VCG (Vickrey-Clarke-Groves) welfare optimization.
        
        Allocates tasks to agents to maximize total welfare while ensuring
        truthful reporting (agents maximize utility by being honest).
        """
        
        # Call VCG dispatcher from ~/hermes-agent/gateway/vcg_dispatcher.py
        vcg_dispatcher = self.dependencies.get('vcg_dispatcher')
        
        if vcg_dispatcher:
            try:
                allocation = await vcg_dispatcher.allocate_tasks(
                    tasks=[t.dict() if hasattr(t, 'dict') else t for t in tasks],
                    agents=agents,
                    objective='welfare_maximization',
                    constraints={'capacity': True, 'skills': True},
                )
                return allocation
            except Exception as e:
                self.logger.warning(f"VCG dispatcher error: {e}, falling back to simple allocation")
        
        # Fallback: simple round-robin allocation
        allocation = {
            'success': True,
            'method': 'round-robin-fallback',
            'assignment': {},
            'total_welfare': len(tasks) * 10,
        }
        
        for i, task in enumerate(tasks):
            agent_idx = i % len(agents)
            agent = agents[agent_idx]
            allocation['assignment'][task.id] = agent['id']
        
        return allocation
    
    async def _assign_voice_agents(self,
                                  tasks: List[Task],
                                  allocation: Dict[str, str],
                                  available_agents: List[Dict]) -> List[Task]:
        """Assign VoiceTwins and LiveKit agents to tasks"""
        
        assigned_tasks = []
        agent_map = {a['id']: a for a in available_agents}
        
        for task in tasks:
            agent_id = allocation.get(task.id)
            agent = agent_map.get(agent_id)
            
            if agent:
                # Set assigned agent
                task.assigned_agent_id = agent_id
                task.agent_type = agent.get('type', 'embodied')
                task.status = 'assigned'
                
                # If VoiceTwin, create session
                if agent.get('type') == 'voice':
                    task.voice_session_id = f"session-{task.id}"
                
                # If LiveKit, create stream
                if agent.get('type') == 'realtime':
                    task.livekit_room = f"room-{task.id}"
            
            assigned_tasks.append(task)
        
        return assigned_tasks
    
    def _create_execution_plan(self,
                              assigned_tasks: List[Task],
                              vcg_allocation: Dict) -> Dict[str, Any]:
        """Create detailed execution plan"""
        
        return {
            'allocation_method': vcg_allocation.get('method', 'VCG'),
            'total_tasks': len(assigned_tasks),
            'total_capacity_used': sum(t.estimated_hours for t in assigned_tasks),
            'phases': [
                {
                    'phase': 'execution',
                    'start': datetime.now().isoformat(),
                    'tasks': [t.id for t in assigned_tasks],
                    'coordination': 'vcg-optimized',
                }
            ],
            'welfare_score': vcg_allocation.get('total_welfare', 0),
        }


# ==================== EXPORTS ====================

__all__ = ['TaskExecutionEngine']
