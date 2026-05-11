P1-005: Remote API Endpoint Implementation
============================================

## Overview

Implemented the POST /api/agent/execute endpoint for Hermes multi-instance architecture,
enabling remote instances to dispatch execution requests with proper authentication,
validation, and error handling.

## Implementation Details

### File: /home/ubuntu/hermes-agent/gateway/remote_agent_api.py

#### 1. Authentication (P1-001 Dependency)

- **Function**: `verify_api_key(x_hermes_key: Optional[str]) -> bool`
- **Security**: Uses `hmac.compare_digest()` for constant-time comparison (prevents timing attacks)
- **Configuration**: Reads from `HERMES_REMOTE_API_KEY` environment variable
- **Caching**: Uses `@lru_cache(maxsize=1)` on `get_expected_key()` for performance

**Example:**
```python
# In environment
export HERMES_REMOTE_API_KEY="your-secret-key-here"

# Usage
if verify_api_key(request.headers.get("X-Hermes-Key")):
    # Request is authenticated
else:
    # Reject with 401 Unauthorized
```

#### 2. Request Validation (P1-005 Core)

**Required Fields:**
- `agent_id` (string): Instance to execute on (e.g., "default", "hermes2", "local")
- `prompt` (string): User prompt/message to execute
- `session_id` (string, optional): Session context for multi-turn conversations

**Validation Rules:**
- `agent_id` must not be empty
- `prompt` must not be empty
- `prompt` length must not exceed 100,000 characters (DoS protection)
- Whitespace is trimmed

**Pydantic Model:**
```python
class ExecuteRequest(BaseModel):
    agent_id: str
    prompt: str
    session_id: Optional[str] = None
```

#### 3. POST /api/agent/execute Endpoint

**HTTP Request:**
```
POST /api/agent/execute HTTP/1.1
Host: hermes-instance:8000
Content-Type: application/json
X-Hermes-Key: your-api-key
X-Hermes-User: optional-username

{
  "agent_id": "default",
  "prompt": "What is AI?",
  "session_id": "user_12345"
}
```

**Successful Response (200 OK):**
```json
{
  "status": "success",
  "output": "AI is artificial intelligence...",
  "error": null,
  "session_id": "user_12345",
  "timestamp": "2026-05-11T04:08:00Z"
}
```

**Authentication Failure (401 Unauthorized):**
```json
{
  "detail": "Unauthorized"
}
```

**Validation Error (400 Bad Request):**
```json
{
  "detail": "prompt is required and cannot be empty"
}
```

**Execution Error (500 Internal Server Error):**
```json
{
  "status": "error",
  "output": null,
  "error": "Instance 'invalid' not found",
  "session_id": "user_12345",
  "timestamp": "2026-05-11T04:08:00Z"
}
```

#### 4. InstanceOrchestrator Integration

The endpoint calls `orchestrator.execute_on_instance()` which handles:

- **Instance Resolution**: Maps agent_id to RemoteHermesInstance
- **Local vs Remote**: Detects if instance is local or remote
- **HTTP Execution**: For remote instances, makes authenticated HTTP call
- **Retry Logic**: Exponential backoff for transient failures
- **Error Handling**: Distinguishes auth failures, timeouts, server errors
- **Connection Management**: Properly closes HTTP connections to prevent leaks

**Flow:**
```
1. Verify API key (P1-001)
2. Validate request (P1-005)
3. Call orchestrator.execute_on_instance(agent_id, prompt, session_id)
4. If returns None (local instance), fall back to gateway_runner.agent.chat()
5. Build response with status, output, error, timestamp
6. Return JSON response
```

#### 5. Error Handling

| Error Type | HTTP Status | Response |
|------------|-------------|----------|
| Invalid API key | 401 | `{"detail": "Unauthorized"}` |
| Missing agent_id | 400 | `{"detail": "agent_id is required"}` |
| Empty prompt | 400 | `{"detail": "prompt is required..."}` |
| Prompt too long | 400 | `{"detail": "prompt exceeds maximum..."}` |
| Instance not found | 500 | `{"status": "error", "error": "..."}` |
| Orchestrator missing | 500 | `{"status": "error", "error": "..."}` |
| Execution timeout | 500 | `{"status": "error", "error": "..."}` |
| Other exceptions | 500 | `{"status": "error", "error": "<truncated>"}` |

#### 6. GET /health Endpoint

Simple health check endpoint for monitoring:

**Request:**
```
GET /health
```

**Response (200 OK):**
```json
{
  "status": "ok",
  "instance": "hermes",
  "timestamp": "2026-05-11T04:08:00Z"
}
```

#### 7. GET /api/agent/status Endpoint

Authenticated endpoint to check agent status:

**Request:**
```
GET /api/agent/status
X-Hermes-Key: your-api-key
```

**Response (200 OK):**
```json
{
  "running": false,
  "current_session": null,
  "model": "claude-3-sonnet",
  "instance": "hermes",
  "timestamp": "2026-05-11T04:08:00Z"
}
```

**Failure (401 Unauthorized):**
```json
{
  "detail": "Unauthorized"
}
```

## Security Features

1. **Authentication** (P1-001):
   - API key required in `X-Hermes-Key` header
   - Constant-time comparison prevents timing attacks
   - Missing/invalid keys return 401

2. **Validation** (P1-005):
   - Required field validation
   - Whitespace trimming
   - Length limits (100KB max prompt) prevent DoS
   - Pydantic models ensure type safety

3. **Error Handling**:
   - Auth failures logged at WARNING level
   - Validation failures logged at WARNING level
   - Execution errors logged at ERROR level with traceback
   - Error messages truncated in response to prevent info disclosure

4. **Resource Management**:
   - InstanceOrchestrator manages HTTP connection pool
   - Response bodies properly consumed
   - Async/await for non-blocking execution

## Testing

### Test File: /home/ubuntu/hermes-agent/tests/gateway/test_remote_agent_api.py

Comprehensive test suite covering:

**Authentication Tests:**
- Valid API key ✓
- Invalid API key ✓
- Missing API key header ✓
- Unconfigured API key ✓
- Constant-time comparison ✓

**Validation Tests:**
- Empty agent_id ✓
- Empty prompt ✓
- Prompt exceeds max length ✓
- Valid request structure ✓

**Integration Tests:**
- InstanceOrchestrator execution ✓
- Local instance fallback ✓
- Remote instance execution ✓

**Response Format Tests:**
- Success response structure ✓
- Error response structure ✓
- Timestamp format ✓

**Error Handling Tests:**
- Auth failure returns 401 ✓
- Missing agent_id returns 400 ✓
- Empty prompt returns 400 ✓
- Execution failure returns error status ✓

**DoS Prevention Tests:**
- Max prompt length protection ✓
- Invalid configuration handling ✓

### Manual Test Results

All 12 tests passing:
```
✓ Auth with valid API key
✓ Auth with invalid API key
✓ Auth with missing API key header
✓ Auth when API key not configured
✓ Request validation - empty agent_id
✓ Request validation - empty prompt
✓ Request validation - max prompt length
✓ Response format - success case
✓ Response format - error case
✓ Constant-time comparison
✓ InstanceOrchestrator integration
✓ Local instance fallback
```

## Usage Examples

### Example 1: Valid Request

```bash
curl -X POST http://localhost:8000/api/agent/execute \
  -H "Content-Type: application/json" \
  -H "X-Hermes-Key: your-secret-key" \
  -H "X-Hermes-User: remote_gateway" \
  -d '{
    "agent_id": "default",
    "prompt": "What is the capital of France?",
    "session_id": "session_123"
  }'
```

**Response:**
```json
{
  "status": "success",
  "output": "The capital of France is Paris.",
  "error": null,
  "session_id": "session_123",
  "timestamp": "2026-05-11T04:08:00Z"
}
```

### Example 2: Authentication Failure

```bash
curl -X POST http://localhost:8000/api/agent/execute \
  -H "Content-Type: application/json" \
  -H "X-Hermes-Key: wrong-key" \
  -d '{"agent_id": "default", "prompt": "test"}'
```

**Response (401):**
```json
{
  "detail": "Unauthorized"
}
```

### Example 3: Validation Error (Empty Prompt)

```bash
curl -X POST http://localhost:8000/api/agent/execute \
  -H "Content-Type: application/json" \
  -H "X-Hermes-Key: your-secret-key" \
  -d '{"agent_id": "default", "prompt": ""}'
```

**Response (400):**
```json
{
  "detail": "prompt is required and cannot be empty"
}
```

### Example 4: Remote Instance Execution

```bash
curl -X POST http://localhost:8000/api/agent/execute \
  -H "Content-Type: application/json" \
  -H "X-Hermes-Key: your-secret-key" \
  -d '{
    "agent_id": "hermes2",
    "prompt": "What is machine learning?",
    "session_id": "session_456"
  }'
```

## Configuration

### Environment Variables

```bash
# API Key for authentication
export HERMES_REMOTE_API_KEY="your-secret-key-here"

# Optional: Configure InstanceOrchestrator instances
# (typically in gateway config)
```

### InstanceOrchestrator Configuration

The orchestrator must be initialized in the gateway:

```python
from gateway.instance_orchestrator import InstanceOrchestrator

orchestrator = InstanceOrchestrator()
await orchestrator.init()

# Make available to gateway_runner
gateway_runner.instance_orchestrator = orchestrator
```

## Files Modified/Created

1. **Modified:** `/home/ubuntu/hermes-agent/gateway/remote_agent_api.py`
   - Complete implementation of POST /api/agent/execute endpoint
   - GET /health and GET /api/agent/status endpoints
   - Authentication, validation, error handling
   - Pydantic models for request/response

2. **Created:** `/home/ubuntu/hermes-agent/tests/gateway/test_remote_agent_api.py`
   - Comprehensive test suite (13 test classes/functions)
   - Tests for auth, validation, integration, error handling
   - Response format validation
   - Mock-based unit tests

3. **Created:** `/home/ubuntu/hermes-agent/test_p1005_manual.py`
   - Standalone test script (12 tests)
   - Runs without pytest plugins
   - Validates all core functionality
   - All tests passing ✓

## Dependencies

The implementation depends on:

1. **P1-001** (Authentication):
   - `verify_api_key()` function using `hmac.compare_digest()`
   - API key from `HERMES_REMOTE_API_KEY` environment variable

2. **InstanceOrchestrator** (gateway/instance_orchestrator.py):
   - `execute_on_instance()` method for execution
   - HTTP client management for remote calls
   - Retry logic and error handling

3. **FastAPI** (optional):
   - For HTTP endpoint routing
   - Pydantic models for validation
   - HTTPException for error responses

4. **Flask** (fallback):
   - Alternative implementation if FastAPI not available
   - Blueprint-based routing

## Future Improvements

1. **Streaming Responses**: Support streaming for long-running tasks
2. **Request Timeout**: Configurable timeout per request
3. **Rate Limiting**: Implement per-user/per-IP rate limiting
4. **Request Logging**: Detailed audit log of all API calls
5. **Response Compression**: Gzip compression for large responses
6. **Health Check Endpoint**: Enhanced with dependencies, queue status
7. **Metrics**: Prometheus metrics for monitoring
8. **Caching**: Response caching for identical requests

## Summary

✓ POST /api/agent/execute endpoint implemented with:
  - Authentication via API key (P1-001)
  - Request validation (agent_id, prompt, length limits)
  - InstanceOrchestrator integration
  - Proper error handling and responses
  - Security features (constant-time comparison, DoS protection)
  - Comprehensive test coverage (12/12 tests passing)

✓ GET /health endpoint for monitoring

✓ GET /api/agent/status endpoint for agent status (authenticated)

✓ Request/Response models with Pydantic validation

✓ Error handling for auth, validation, and execution failures

✓ Documentation with examples and usage guide
