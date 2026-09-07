---
name: test-skill-with-tool
description: Test skill demonstrating tool registration from SKILL.md
version: 1.0.0
metadata:
  hermes:
    tools:
      - name: echo_message
        description: Echo back a provided message
        toolset: test-skill
        schema:
          type: object
          properties:
            message:
              type: string
              description: The message to echo back
          required: [message]
        handler_module: test_skill_handlers
        handler_function: handle_echo_message
        requires_env: []
        is_async: false
        emoji: 📢
      
      - name: add_numbers
        description: Add two numbers together
        toolset: test-skill
        schema:
          type: object
          properties:
            a:
              type: number
              description: First number
            b:
              type: number
              description: Second number
          required: [a, b]
        handler_module: test_skill_handlers
        handler_function: handle_add_numbers
        requires_env: []
        is_async: false
        emoji: ➕
---

# Test Skill with Tool Definitions

This skill demonstrates the VOICE-TOOL-04 feature: tools registered directly within SKILL.md files.

## Custom Tools

This skill provides two tools:
- `echo_message` — echoes back a provided message
- `add_numbers` — adds two numbers together

Both tools are discovered automatically by the skill tool discovery system.
