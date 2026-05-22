"""Integration example: Using VCG dispatcher in hermes-agent gateway.

This file shows practical code patterns for integrating VCGDispatcher
into the gateway/run.py flow and remote_agent_api.py health endpoints.
"""

# ─────────────────────────────────────────────────────────────────────────
# Example 1: Initialize VCG Gateway in gateway/run.py startup
# ─────────────────────────────────────────────────────────────────────────

"""
# In gateway/run.py, during FastAPI app startup:

from gateway.vcg_gateway import initialize_vcg_gateway, get_vcg_gateway
import yaml

@app.on_event("startup")
async def startup_vcg_gateway():
    '''Initialize VCG dispatcher during gateway startup.'''
    config = yaml.safe_load(open(os.path.expanduser('~/.hermes/config.yaml')))
    vcg_config = config.get('vcg', {})
    
    if vcg_config.get('enabled'):
        try:
            gateway = initialize_vcg_gateway(vcg_config)
            logger.info("[VCG] Gateway initialized")
            
            # Start async connection and heartbeat monitoring
            await gateway.connect()
        except Exception as e:
            logger.warning(f"[VCG] Failed to initialize: {e}")

@app.on_event("shutdown")
async def shutdown_vcg_gateway():
    '''Clean up VCG gateway on shutdown.'''
    vcg = get_vcg_gateway()
    if vcg:
        await vcg.close()
"""


# ─────────────────────────────────────────────────────────────────────────
# Example 2: Use VCG in instance orchestrator dispatch
# ─────────────────────────────────────────────────────────────────────────

"""
# In gateway/instance_orchestrator.py, in dispatch_command():

from gateway.vcg_gateway import get_vcg_gateway
import asyncio

async def dispatch_command(user_id, platform, chat_id, message_text, ...):
    '''Dispatch message to best Hermes instance using VCG allocation.'''
    
    # Extract required skills from message
    required_skills = extract_skills_from_message(message_text)
    
    # Try VCG allocation if available
    vcg = get_vcg_gateway()
    if vcg:
        try:
            allocation = await vcg.allocate_task(
                task_id=f"msg_{user_id}_{int(time.time()*1000)}",
                title=f"[{platform}] {message_text[:80]}",
                required_skills=required_skills,
                reach=5,        # User sent message
                impact=5,       # Medium importance
                confidence=0.8, # We're usually right about message type
                effort=0.5,     # Quick response expected
            )
            
            if allocation:
                instance_id = allocation.agent_id
                logger.info(
                    f"[VCG] Allocated {allocation.task_id} to {instance_id} "
                    f"(welfare={allocation.welfare:.1f}, tax={allocation.clarke_tax:.1f})"
                )
            else:
                # Fallback if no agent ready
                instance_id = get_default_instance()
                logger.warning(f"[VCG] No agent ready, using fallback: {instance_id}")
        except Exception as e:
            logger.error(f"[VCG] Allocation failed: {e}")
            instance_id = get_default_instance()
    else:
        # VCG not enabled, use static instance
        instance_id = get_default_instance()
    
    # Dispatch to selected instance
    response = await run_remote_agent(
        instance_id=instance_id,
        message=message_text,
        user_id=user_id,
        platform=platform,
        chat_id=chat_id,
    )
    
    # Notify VCG that task completed
    if vcg:
        task_id = f"msg_{user_id}_{int(time.time()*1000)}"
        vcg.report_task_completion(task_id)
    
    return response
"""


# ─────────────────────────────────────────────────────────────────────────
# Example 3: Report health from remote_agent_api /health endpoint
# ─────────────────────────────────────────────────────────────────────────

"""
# In gateway/remote_agent_api.py, in /health endpoint:

from gateway.vcg_gateway import get_vcg_gateway

@app.get("/health")
async def health_check():
    '''Health check endpoint with VCG integration.'''
    
    # Calculate current health metrics
    health_data = {
        "status": "healthy",
        "uptime_seconds": get_uptime_seconds(),
        "memory_percent": psutil.virtual_memory().percent,
        "cpu_percent": psutil.cpu_percent(interval=1),
        "response_time_ms": get_avg_response_time_ms(),
        "error_rate": get_error_rate(),
        "active_tasks": get_active_task_count(),
        "available_slots": AGENT_CAPACITY - get_active_task_count(),
    }
    
    # Calculate health score for VCG
    health_score = calculate_vcg_health_score(health_data)
    
    # Report to VCG dispatcher if available
    vcg = get_vcg_gateway()
    if vcg:
        try:
            vcg.report_node_health(
                agent_id=get_instance_id(),
                health_score=health_score,
                capacity_available=health_data['available_slots'],
                response_time_ms=health_data['response_time_ms'],
            )
            logger.debug(f"[VCG] Reported health: score={health_score:.2f}")
        except Exception as e:
            logger.warning(f"[VCG] Failed to report health: {e}")
    
    return health_data


def calculate_vcg_health_score(health_data: dict) -> float:
    '''Calculate 0-1 health score for VCG from metrics.'''
    score = 1.0
    
    # Degrade for high CPU/memory
    if health_data['cpu_percent'] > 80:
        score -= 0.2
    elif health_data['cpu_percent'] > 60:
        score -= 0.1
    
    if health_data['memory_percent'] > 80:
        score -= 0.2
    elif health_data['memory_percent'] > 60:
        score -= 0.1
    
    # Degrade for high error rate
    error_rate = health_data['error_rate']
    if error_rate > 0.05:
        score -= min(0.3, error_rate * 5)
    
    # Degrade for slow response time
    response_time = health_data['response_time_ms']
    if response_time > 5000:
        score -= 0.3
    elif response_time > 2000:
        score -= 0.1
    
    return max(0.0, min(1.0, score))
"""


# ─────────────────────────────────────────────────────────────────────────
# Example 4: Monitoring dashboard integration
# ─────────────────────────────────────────────────────────────────────────

"""
# In gateway/run.py, add VCG monitoring endpoints:

from gateway.vcg_gateway import get_vcg_gateway

@app.get("/vcg/health")
async def vcg_health_report():
    '''VCG health report for monitoring dashboard.'''
    vcg = get_vcg_gateway()
    if not vcg:
        return {"error": "VCG not enabled"}
    return vcg.get_health_status()

@app.get("/vcg/accounting")
async def vcg_resource_accounting():
    '''VCG resource accounting for billing system.'''
    vcg = get_vcg_gateway()
    if not vcg:
        return {"error": "VCG not enabled"}
    return vcg.get_resource_accounting()

@app.post("/vcg/task-complete")
async def vcg_task_complete(task_id: str):
    '''Mark a task as complete and free capacity.'''
    vcg = get_vcg_gateway()
    if not vcg:
        return {"error": "VCG not enabled"}
    success = vcg.report_task_completion(task_id)
    return {"task_id": task_id, "completed": success}
"""


# ─────────────────────────────────────────────────────────────────────────
# Example 5: Manual skill extraction from message
# ─────────────────────────────────────────────────────────────────────────

"""
def extract_skills_from_message(message: str) -> list:
    '''Extract required skills from message content.'''
    message_lower = message.lower()
    skills = []
    
    # Detect skill keywords
    if any(w in message_lower for w in ['image', 'vision', 'see', 'look', 'photo']):
        skills.append('vision')
    
    if any(w in message_lower for w in ['code', 'program', 'python', 'javascript']):
        skills.append('code-review')
    
    if any(w in message_lower for w in ['math', 'calculate', 'algebra', 'number']):
        skills.append('reasoning')
    
    if any(w in message_lower for w in ['write', 'essay', 'article', 'blog']):
        skills.append('writing')
    
    # Default to generic if no specific skills detected
    if not skills:
        skills = ['general']
    
    return skills
"""


# ─────────────────────────────────────────────────────────────────────────
# Example 6: Configuration (config.yaml)
# ─────────────────────────────────────────────────────────────────────────

"""
# Add to ~/.hermes/config.yaml:

vcg:
  enabled: true
  
  agents:
    - id: hermes-local
      skills: [all]
      capacity: 5
      reliability: 0.95
      
    - id: hermes2
      skills: [vision, code-review]
      capacity: 3
      reliability: 0.9
      
    - id: hermes3
      skills: [reasoning, math]
      capacity: 4
      reliability: 0.92
  
  # NATS servers for event publishing
  nats_servers:
    - nats://localhost:4222
  
  # Heartbeat interval (seconds)
  heartbeat_interval_sec: 10
  
  # Stale detection thresholds
  stale_timeout_degraded_sec: 30
  stale_timeout_unhealthy_sec: 60
"""


# ─────────────────────────────────────────────────────────────────────────
# Example 7: Testing the integration locally
# ─────────────────────────────────────────────────────────────────────────

"""
# Test VCG allocation locally:

if __name__ == "__main__":
    import asyncio
    from gateway.vcg_gateway import VCGGateway, VCGAgent
    
    # Create test agents
    agents = [
        VCGAgent(id="hermes-local", skills=["all"], capacity=5),
        VCGAgent(id="hermes2", skills=["vision", "code-review"], capacity=3),
        VCGAgent(id="hermes3", skills=["reasoning"], capacity=4),
    ]
    
    # Create and initialize gateway
    gateway = VCGGateway(
        dispatcher=VCGDispatcher(agents=agents),
        nats_servers=["nats://localhost:4222"],
    )
    
    async def test():
        async with gateway.connect():
            # Report health from remote instances
            gateway.report_node_health("hermes-local", health_score=0.95)
            gateway.report_node_health("hermes2", health_score=0.85)
            gateway.report_node_health("hermes3", health_score=0.90)
            
            # Allocate a vision task
            result = await gateway.allocate_task(
                task_id="t_vision_001",
                title="Analyze image for content moderation",
                required_skills=["vision"],
                reach=8, impact=9, confidence=0.9, effort=2.0,
            )
            
            if result:
                print(f"✅ Allocated to {result.agent_id}")
                print(f"   Welfare: {result.welfare:.1f}")
                print(f"   Clarke tax: {result.clarke_tax:.1f}")
            
            # Get health report
            health = gateway.get_health_status()
            print(f"Nodes ready: {health['summary']['healthy_nodes']}")
            print(f"Utilization: {health['summary']['capacity_utilization']:.1%}")
    
    asyncio.run(test())
"""
