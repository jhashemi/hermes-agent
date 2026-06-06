# Skill Tool Registration — User Guide

## Overview

VOICE-TOOL-04 enables you to register custom tools directly within SKILL.md files. Tools are auto-discovered and registered at startup, making them available to all agents (text and voice).

## Why Register Tools in Skills?

- **Co-location**: Tool definition and implementation live together
- **Auto-discovery**: No separate registration step needed
- **Versioning**: Tools are part of the skill version
- **Distribution**: Share skills with their custom tools via the skills hub
- **Isolation**: Tool handlers are scoped to skill directories

## Creating a Skill with Custom Tools

### Step 1: Create Skill Directory

```bash
mkdir -p ~/.hermes/skills/my-custom-skill
```

### Step 2: Define Tools in SKILL.md

Add your tool definitions to the `metadata.hermes.tools` array in SKILL.md:

```yaml
---
name: my-custom-skill
description: My skill with custom tools
version: 1.0.0
metadata:
  hermes:
    tools:
      - name: analyze_text
        description: Analyze text for sentiment and entities
        toolset: my-custom-skill
        schema:
          type: object
          properties:
            text:
              type: string
              description: Text to analyze
            language:
              type: string
              description: Language code (e.g., 'en', 'fr')
              default: en
          required: [text]
        handler_module: text_analyzers
        handler_function: handle_analyze_text
        requires_env: [ANALYSIS_API_KEY]
        is_async: false
        emoji: 📝
---

# My Custom Skill

This skill provides text analysis tools.

## Tools

- `analyze_text` — Analyze text for sentiment and named entities
```

### Step 3: Implement Handler Module

Create `~/.hermes/skills/my-custom-skill/text_analyzers.py`:

```python
#!/usr/bin/env python3
"""Text analysis handlers for my-custom-skill."""

import json
import os


def handle_analyze_text(args: dict, **kwargs) -> str:
    """Analyze text for sentiment and entities.
    
    Args:
        args: Dict with keys:
            - text (str, required): Text to analyze
            - language (str, optional): Language code
    
    Returns:
        JSON string with analysis results
    """
    text = args.get("text", "")
    language = args.get("language", "en")
    
    if not text:
        return json.dumps({"error": "text parameter required"})
    
    # Call your analysis API
    api_key = os.getenv("ANALYSIS_API_KEY")
    if not api_key:
        return json.dumps({"error": "ANALYSIS_API_KEY not configured"})
    
    # TODO: Call actual analysis service
    # This is a stub example:
    
    return json.dumps({
        "success": True,
        "text": text,
        "language": language,
        "sentiment": "positive",
        "entities": [],
        "analysis": "Text analyzed successfully",
    })
```

### Step 4: Test Tool Discovery

Restart any Hermes agent or reload the module. The tool is automatically discovered:

```bash
$ python -c "from model_tools import registry; \
  tools = registry.get_tool_names_for_toolset('my-custom-skill'); \
  print('Discovered tools:', tools)"

Discovered tools: ['analyze_text']
```

### Step 5: Use the Tool

In agent conversations:

```
User: /skills load my-custom-skill
Agent: ✓ Skill loaded

User: Analyze this text: "I love Hermes!"
Agent: [calls analyze_text tool]
        Result: sentiment=positive, entities=["Hermes"]
```

## Tool Metadata Reference

### Required Fields

- **name** (str): Tool identifier (lowercase, underscores)
- **description** (str): Human-readable description
- **toolset** (str): Toolset name (appears in `hermes tools` output)
- **schema** (object): OpenAI function schema
- **handler_module** (str): Python module name (in skill directory)
- **handler_function** (str): Function name in that module

### Optional Fields

- **requires_env** (list): Environment variable names required for tool
  - Example: `["API_KEY", "SECRET_TOKEN"]`
  - Used by `hermes tools` to show setup instructions
- **is_async** (bool): Whether handler is async/await (default: false)
  - If true, handler should return a coroutine
- **emoji** (str): Emoji for CLI display (default: 📝)
- **max_result_size_chars** (int): Max chars in tool output (default: 100000)

## Schema Format

Tools must define a valid OpenAI function schema. Minimal example:

```yaml
schema:
  type: object
  properties:
    param1:
      type: string
      description: First parameter
    param2:
      type: number
      description: Second parameter
  required: [param1]  # Required parameters
```

Supported types: `string`, `number`, `integer`, `boolean`, `array`, `object`

Full OpenAI schema docs: https://platform.openai.com/docs/api-reference/chat/create#function-schema

## Handler Function Signature

Handlers must accept a dict and return a JSON string:

```python
def handle_my_tool(args: dict, **kwargs) -> str:
    """
    Args:
        args: Dict containing validated parameters from schema
        **kwargs: Reserved for future use (task_id, user_id, etc.)
    
    Returns:
        JSON string (success or error format)
    """
    try:
        result = do_something(args.get("param1"))
        return json.dumps({"success": True, "result": result})
    except Exception as e:
        return json.dumps({"error": str(e)})
```

### Response Formats

**Success:**
```json
{"success": true, "result": "..."}
```

**Error:**
```json
{"error": "error message"}
```

**Using Helpers:**
```python
from tools.registry import tool_result, tool_error

# Success
return tool_result({"data": "..."})

# Error
return tool_error("Something went wrong", code=400)
```

## Environment Variables

If your tool requires API keys or configuration:

1. Declare them in SKILL.md:
   ```yaml
   requires_env: [MY_API_KEY]
   ```

2. Read them in your handler:
   ```python
   import os
   api_key = os.getenv("MY_API_KEY")
   ```

3. Set them before running agent:
   ```bash
   export MY_API_KEY=your_key_here
   hermes chat
   ```

4. Check status:
   ```bash
   hermes tools check
   # Shows which required env vars are configured
   ```

## Error Handling

### Missing Handler Module

```
[ERROR] Could not find handler module my_module in skill my-skill
```

**Fix:** Ensure `my_handlers.py` exists in skill directory

### Bad Schema

```
[WARNING] Tool my_tool in skill my-skill has invalid schema
```

**Fix:** Schema must have `type: object` and `properties` dict

### Handler Not Callable

```
[ERROR] Handler function handle_my_tool is not callable
```

**Fix:** Ensure function is defined at module level (not inside another function)

## Best Practices

### 1. Validate Parameters

```python
def handle_my_tool(args: dict, **kwargs) -> str:
    param = args.get("param")
    if not param:
        return tool_error("param is required")
    
    if len(param) > 1000:
        return tool_error("param must be < 1000 characters")
    
    # Safe to proceed
```

### 2. Handle Async Operations

For long operations, use `is_async: true`:

```yaml
- name: process_file
  is_async: true
  handler_module: file_processors
  handler_function: handle_process_file

---

async def handle_process_file(args: dict, **kwargs) -> str:
    filename = args.get("filename")
    result = await some_async_operation(filename)
    return tool_result(result)
```

### 3. Set Result Size Limits

For tools that might return large data:

```yaml
- name: search_documents
  max_result_size_chars: 50000  # Cap at 50KB
```

### 4. Document Examples in SKILL.md

```yaml
---
name: my-tool
...
---

# My Tool Skill

## Example Usage

### Analyze Sentiment

> User: Analyze this: "I love this tool!"

The tool returns:
```json
{"sentiment": "positive", "score": 0.95}
```
```

## Troubleshooting

### Tool Not Discovered

1. Check skill directory structure:
   ```
   ~/.hermes/skills/my-skill/
   ├── SKILL.md
   └── my_handlers.py
   ```

2. Verify SKILL.md has valid YAML frontmatter:
   ```bash
   hermes skills view my-skill | head -20
   ```

3. Check logs:
   ```bash
   hermes logs --level DEBUG | grep "skill tool"
   ```

### Tool Fails on Execution

1. Check handler error output:
   ```bash
   # In agent chat
   User: Call my_tool with test data
   # Look for {"error": "..."} in response
   ```

2. Verify handler imports:
   ```bash
   python -c "from my_skill.my_handlers import handle_my_tool"
   ```

3. Test handler directly:
   ```python
   from my_skill.my_handlers import handle_my_tool
   result = handle_my_tool({"param": "test"})
   print(result)  # Should be valid JSON
   ```

## Examples

### Simple Echo Tool

```yaml
- name: echo
  description: Echo back your message
  toolset: utils
  schema:
    type: object
    properties:
      message:
        type: string
    required: [message]
  handler_module: utils
  handler_function: handle_echo
```

```python
# utils.py
import json

def handle_echo(args: dict, **kwargs) -> str:
    return json.dumps({"echo": args.get("message", "")})
```

### API Integration Tool

```yaml
- name: weather
  description: Get current weather
  requires_env: [OPENWEATHER_API_KEY]
  schema:
    type: object
    properties:
      city:
        type: string
      units:
        type: string
        enum: [metric, imperial]
        default: metric
    required: [city]
  handler_module: weather
  handler_function: handle_weather
```

```python
# weather.py
import json
import os
import requests

def handle_weather(args: dict, **kwargs) -> str:
    city = args.get("city")
    units = args.get("units", "metric")
    api_key = os.getenv("OPENWEATHER_API_KEY")
    
    if not api_key:
        return json.dumps({"error": "OPENWEATHER_API_KEY not set"})
    
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&units={units}&appid={api_key}"
        resp = requests.get(url)
        data = resp.json()
        
        return json.dumps({
            "success": True,
            "city": city,
            "temp": data["main"]["temp"],
            "description": data["weather"][0]["description"],
        })
    except Exception as e:
        return json.dumps({"error": str(e)})
```

---

**Next Steps:**
- Create your skill with custom tools
- Test with `hermes skills view my-skill`
- Use tools in agent conversations
- Share via Hermes Skills Hub (coming soon)
