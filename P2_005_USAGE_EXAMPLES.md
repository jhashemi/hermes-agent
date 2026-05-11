# P2-005: Usage Examples and Edge Cases

## Usage Examples

### Basic Usage (With Defaults)
```python
from gateway.instance_orchestrator import get_instance_config

# No environment variables set - uses defaults
config = get_instance_config()
print(config)
# Output:
# {
#     'remote_api_key': '',
#     'instance_a_hostname': 'localhost',
#     'instance_a_port': 8000
# }
```

### With Environment Variables
```bash
# Set environment variables
export HERMES_REMOTE_API_KEY="my-secret-key"
export HERMES_INSTANCE_A_HOSTNAME="instance-a.example.com"
export HERMES_INSTANCE_A_PORT="9000"

# Run Python code
python -c "
from gateway.instance_orchestrator import get_instance_config
config = get_instance_config()
print(config)
# Output:
# {
#     'remote_api_key': 'my-secret-key',
#     'instance_a_hostname': 'instance-a.example.com',
#     'instance_a_port': 9000
# }
"
```

### Dynamic Configuration Changes (No Restart)
```python
import os
from gateway.instance_orchestrator import get_instance_config

# Initial configuration
config1 = get_instance_config()
print(f"First call: {config1['instance_a_hostname']}")  # localhost

# Change environment without restarting
os.environ['HERMES_INSTANCE_A_HOSTNAME'] = "new-host.local"

# Next call picks up the change immediately
config2 = get_instance_config()
print(f"Second call: {config2['instance_a_hostname']}")  # new-host.local
```

### In InstanceOrchestrator Methods
```python
import asyncio
from gateway.instance_orchestrator import InstanceOrchestrator

async def example():
    orchestrator = InstanceOrchestrator()
    await orchestrator.init()
    
    # execute_on_instance loads config at runtime
    result = await orchestrator.execute_on_instance(
        instance_name='hermes2',
        prompt='What is 2+2?'
    )
    
    # get_instance_status loads config at runtime
    status = await orchestrator.get_instance_status('hermes2')
    
    await orchestrator.close()

asyncio.run(example())
```

## Edge Cases Handled

### 1. Invalid Port Number (Non-Integer)
```bash
export HERMES_INSTANCE_A_PORT="not-a-number"
```
**Behavior**: Logs warning, uses default port 8000
**Log**: "Invalid HERMES_INSTANCE_A_PORT value: invalid literal for int() with base 10: 'not-a-number', using default: 8000"

### 2. Out-of-Range Port (> 65535)
```bash
export HERMES_INSTANCE_A_PORT="99999"
```
**Behavior**: Logs warning, uses default port 8000
**Log**: "Invalid HERMES_INSTANCE_A_PORT value: Port 99999 is out of valid range (1-65535), using default: 8000"

### 3. Empty or Whitespace Hostname
```bash
export HERMES_INSTANCE_A_HOSTNAME="    "
```
**Behavior**: Logs warning, uses default 'localhost'
**Log**: "HERMES_INSTANCE_A_HOSTNAME is empty, using default: localhost"

### 4. Port at Boundary Values
```bash
# Valid: port 1
export HERMES_INSTANCE_A_PORT="1"

# Valid: port 65535
export HERMES_INSTANCE_A_PORT="65535"

# Invalid: port 0
export HERMES_INSTANCE_A_PORT="0"  # Uses default 8000

# Invalid: port 65536
export HERMES_INSTANCE_A_PORT="65536"  # Uses default 8000
```

### 5. Missing Environment Variables
```bash
# No env vars set
unset HERMES_REMOTE_API_KEY
unset HERMES_INSTANCE_A_HOSTNAME
unset HERMES_INSTANCE_A_PORT
```
**Behavior**: All defaults are used
- `remote_api_key` = ''
- `instance_a_hostname` = 'localhost'
- `instance_a_port` = 8000

### 6. Partial Environment Variables
```bash
export HERMES_REMOTE_API_KEY="my-key"
# HERMES_INSTANCE_A_HOSTNAME not set (uses default)
export HERMES_INSTANCE_A_PORT="9000"
```
**Result**:
- `remote_api_key` = 'my-key'
- `instance_a_hostname` = 'localhost'
- `instance_a_port` = 9000

### 7. API Key with Whitespace
```bash
export HERMES_REMOTE_API_KEY="  my-key-with-spaces  "
```
**Result**: Whitespace is stripped, `remote_api_key` = 'my-key-with-spaces'

## Valid Hostname Examples

### Accepted Hostnames
- `localhost` ✓
- `127.0.0.1` ✓
- `192.168.1.1` ✓
- `example.com` ✓
- `host.example.local` ✓
- `hermes2.flounder-snake.ts.net` ✓
- `::1` (IPv6) ✓

### Rejected Hostnames
- `` (empty) ✗
- `   ` (whitespace only) ✗
- `192.168.1` (incomplete IP) ✗
- `256.1.1.1` (invalid IP octet) ✗

## Logging Behavior

### Debug Level (when enabled)
```
Loaded instance config at runtime: hostname=localhost, port=8000
Loaded instance config at runtime in get_instance_status: hostname=localhost, port=8000
HERMES_REMOTE_API_KEY not set in environment
```

### Warning Level
```
HERMES_INSTANCE_A_HOSTNAME is empty, using default: localhost
Invalid HERMES_INSTANCE_A_PORT value: invalid literal for int() with base 10: 'xyz', using default: 8000
Invalid HERMES_INSTANCE_A_PORT value: Port 99999 is out of valid range (1-65535), using default: 8000
```

## Testing

Run comprehensive tests:
```bash
cd /home/ubuntu/hermes-agent
python test_p2005_runtime_env.py
```

Or with pytest (11 tests will pass, 3 will be skipped for async):
```bash
pytest test_p2005_runtime_env.py -v
```

## Migration Guide

### For Existing Code
If you're already using the `InstanceOrchestrator`:
1. No changes required
2. Environment variables are now read at **each call** instead of once at startup
3. You can now update env vars without restarting

### Deploying with Custom Configuration
```bash
# Set your custom values before running your application
export HERMES_REMOTE_API_KEY="your-secret-key"
export HERMES_INSTANCE_A_HOSTNAME="your-instance.local"
export HERMES_INSTANCE_A_PORT="8080"

# Run your application
python your_app.py
```

### Updating Configuration at Runtime (Advanced)
```python
import os
import asyncio
from gateway.instance_orchestrator import InstanceOrchestrator, get_instance_config

async def update_config_demo():
    orchestrator = InstanceOrchestrator()
    await orchestrator.init()
    
    # Initially use localhost
    config1 = get_instance_config()
    print(f"Config 1: {config1['instance_a_hostname']}")  # localhost
    
    # Update environment variable
    os.environ['HERMES_INSTANCE_A_HOSTNAME'] = 'production.instance.com'
    
    # Next call picks up the change
    config2 = get_instance_config()
    print(f"Config 2: {config2['instance_a_hostname']}")  # production.instance.com
    
    # Use in orchestrator (also loads fresh config)
    status = await orchestrator.get_instance_status('local')
    
    await orchestrator.close()

asyncio.run(update_config_demo())
```
