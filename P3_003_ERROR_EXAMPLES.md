P3-003: Error Response Examples and Usage Guide
================================================

This document shows concrete examples of how the standardized error responses
are used across the gateway endpoints.

EXAMPLE 1: Access Denied Error (Authentication Failure)
========================================================

Scenario: User without permission tries to load an agent

Source Code (gateway/agent_commands.py):
  access_mgr = get_access_manager()
  if not access_mgr.has_access(event):
      user_id = access_mgr.get_user_id(event)
      error = create_access_denied_error(
          user_id=user_id,
          command="load-agent",
          reason="You don't have permission to load agent personas.",
      )
      return error.to_emoji_response()

ErrorResponse Object:
  ErrorResponse(
      code=ErrorCode.ACCESS_DENIED,
      message="Access Denied. You don't have permission to perform this action.",
      context={
          "user_id": "unknown_user",
          "command": "load-agent",
          "reason": "You don't have permission to load agent personas."
      },
      severity=ErrorSeverity.MEDIUM.value,
      user_id="unknown_user",
      command="load-agent"
  )

Chat Response:
  🚫 Access Denied. You don't have permission to perform this action.
  
  Your ID: unknown_user

Dictionary (for APIs):
  {
      "code": "ACCESS_DENIED",
      "message": "Access Denied. You don't have permission to perform this action.",
      "context": {
          "user_id": "unknown_user",
          "command": "load-agent",
          "reason": "You don't have permission to load agent personas."
      },
      "severity": "medium",
      "user_id": "unknown_user",
      "command": "load-agent"
  }


EXAMPLE 2: Validation Error (Invalid Input)
=============================================

Scenario: User tries to grant access with invalid user ID

Source Code (gateway/access_control.py):
  is_valid, error_msg = validate_user_id(user_id)
  if not is_valid:
      error = create_validation_error(
          field="user_id",
          reason=error_msg,
          user_id=requester_id,
      )
      return error.to_emoji_response()

ErrorResponse Object:
  ErrorResponse(
      code=ErrorCode.INVALID_INPUT,
      message="Validation failed for 'user_id': User ID exceeds maximum length of 256 characters",
      context={
          "field": "user_id",
          "reason": "User ID exceeds maximum length of 256 characters"
      },
      severity=ErrorSeverity.LOW.value,
      user_id="taylor_swanson"
  )

Chat Response:
  ❌ Validation failed for 'user_id': User ID exceeds maximum length of 256 characters

Dictionary (for APIs):
  {
      "code": "INVALID_INPUT",
      "message": "Validation failed for 'user_id': User ID exceeds maximum length of 256 characters",
      "context": {
          "field": "user_id",
          "reason": "User ID exceeds maximum length of 256 characters"
      },
      "severity": "low",
      "user_id": "taylor_swanson",
      "command": null
  }


EXAMPLE 3: Not Found Error (Missing Resource)
==============================================

Scenario: User tries to load a non-existent agent persona

Source Code (gateway/agent_commands.py):
  if persona_key not in EXECUTIVE_PERSONAS:
      error = create_not_found_error(
          resource_type="agent",
          resource_id=persona_key,
          user_id=access_mgr.get_user_id(event),
      )
      return error.to_emoji_response()

ErrorResponse Object:
  ErrorResponse(
      code=ErrorCode.AGENT_NOT_FOUND,
      message="Agent 'unknown_persona' not found.",
      context={
          "resource_type": "agent",
          "resource_id": "unknown_persona"
      },
      severity=ErrorSeverity.LOW.value,
      user_id="taylor_swanson"
  )

Chat Response:
  ❌ Agent 'unknown_persona' not found.

Dictionary (for APIs):
  {
      "code": "AGENT_NOT_FOUND",
      "message": "Agent 'unknown_persona' not found.",
      "context": {
          "resource_type": "agent",
          "resource_id": "unknown_persona"
      },
      "severity": "low",
      "user_id": "taylor_swanson",
      "command": null
  }


EXAMPLE 4: Not Found Error (Missing Instance)
==============================================

Scenario: User tries to switch to a non-existent instance

Source Code (gateway/agent_commands.py):
  if not success:
      error = create_not_found_error(
          resource_type="instance",
          resource_id=instance_name,
          user_id=access_mgr.get_user_id(event),
      )
      return error.to_emoji_response()

ErrorResponse Object:
  ErrorResponse(
      code=ErrorCode.INSTANCE_NOT_FOUND,
      message="Instance 'hermes-999' not found.",
      context={
          "resource_type": "instance",
          "resource_id": "hermes-999"
      },
      severity=ErrorSeverity.LOW.value,
      user_id="taylor_swanson"
  )

Chat Response:
  ❌ Instance 'hermes-999' not found.

Dictionary (for APIs):
  {
      "code": "INSTANCE_NOT_FOUND",
      "message": "Instance 'hermes-999' not found.",
      "context": {
          "resource_type": "instance",
          "resource_id": "hermes-999"
      },
      "severity": "low",
      "user_id": "taylor_swanson",
      "command": null
  }


EXAMPLE 5: Internal Error (Operation Failure)
==============================================

Scenario: Persona manager fails to load agent

Source Code (gateway/agent_commands.py):
  success = persona_mgr.set_persona(persona_key)
  if not success:
      error = ErrorResponse(
          code=ErrorCode.OPERATION_FAILED,
          message=f"Could not load agent: {persona_key}",
          context={"agent": persona_key},
          severity=ErrorSeverity.HIGH.value,
          user_id=access_mgr.get_user_id(event),
      )
      return error.to_emoji_response()

ErrorResponse Object:
  ErrorResponse(
      code=ErrorCode.OPERATION_FAILED,
      message="Could not load agent: demis_hassabis",
      context={"agent": "demis_hassabis"},
      severity=ErrorSeverity.HIGH.value,
      user_id="taylor_swanson",
      command=None
  )

Chat Response:
  ❌ Could not load agent: demis_hassabis

Dictionary (for APIs):
  {
      "code": "OPERATION_FAILED",
      "message": "Could not load agent: demis_hassabis",
      "context": {"agent": "demis_hassabis"},
      "severity": "high",
      "user_id": "taylor_swanson",
      "command": null
  }


EXAMPLE 6: Validation Error (Invalid Instance Name)
====================================================

Scenario: User tries to switch to instance with invalid name format

Source Code (gateway/agent_commands.py):
  is_valid, error_msg = validate_instance_name(instance_name)
  if not is_valid:
      error = create_validation_error(
          field="instance_name",
          reason=error_msg,
          user_id=access_mgr.get_user_id(event),
      )
      return error.to_emoji_response()

ErrorResponse Object (max length exceeded):
  ErrorResponse(
      code=ErrorCode.INVALID_INPUT,
      message="Validation failed for 'instance_name': Instance name must not exceed 64 characters (got 128)",
      context={
          "field": "instance_name",
          "reason": "Instance name must not exceed 64 characters (got 128)"
      },
      severity=ErrorSeverity.LOW.value,
      user_id="taylor_swanson"
  )

Chat Response:
  ❌ Validation failed for 'instance_name': Instance name must not exceed 64 characters (got 128)

ErrorResponse Object (invalid characters):
  ErrorResponse(
      code=ErrorCode.INVALID_INPUT,
      message="Validation failed for 'instance_name': Instance name can only contain alphanumeric characters (a-z, A-Z, 0-9) and hyphens (-)",
      context={
          "field": "instance_name",
          "reason": "Instance name can only contain alphanumeric characters (a-z, A-Z, 0-9) and hyphens (-)"
      },
      severity=ErrorSeverity.LOW.value,
      user_id="taylor_swanson"
  )

Chat Response:
  ❌ Validation failed for 'instance_name': Instance name can only contain alphanumeric characters (a-z, A-Z, 0-9) and hyphens (-)


EXAMPLE 7: Command Permission Check
====================================

Scenario: User without admin permission tries to revoke access

Source Code (gateway/access_control.py):
  requester_id = manager.get_user_id(event)
  if requester_id not in DEFAULT_WHITELIST:
      error = create_access_denied_error(
          user_id=requester_id,
          command="access-revoke",
          reason="Only administrators can revoke access",
      )
      return error.to_emoji_response()

ErrorResponse Object:
  ErrorResponse(
      code=ErrorCode.ACCESS_DENIED,
      message="Access Denied. You don't have permission to perform this action.",
      context={
          "user_id": "random_user",
          "command": "access-revoke",
          "reason": "Only administrators can revoke access"
      },
      severity=ErrorSeverity.MEDIUM.value,
      user_id="random_user",
      command="access-revoke"
  )

Chat Response:
  🚫 Access Denied. You don't have permission to perform this action.
  
  Your ID: random_user

Dictionary (for APIs):
  {
      "code": "ACCESS_DENIED",
      "message": "Access Denied. You don't have permission to perform this action.",
      "context": {
          "user_id": "random_user",
          "command": "access-revoke",
          "reason": "Only administrators can revoke access"
      },
      "severity": "medium",
      "user_id": "random_user",
      "command": "access-revoke"
  }


ERROR CODE REFERENCE
====================

Access Denied:
  • Code: ACCESS_DENIED
  • HTTP Status: 403
  • Severity: MEDIUM
  • Emoji: 🚫
  • Use when: User lacks permission for action

Not Found (Agent):
  • Code: AGENT_NOT_FOUND
  • HTTP Status: 404
  • Severity: LOW
  • Emoji: ❌
  • Use when: Referenced agent persona doesn't exist

Not Found (Instance):
  • Code: INSTANCE_NOT_FOUND
  • HTTP Status: 404
  • Severity: LOW
  • Emoji: ❌
  • Use when: Referenced instance doesn't exist

Invalid Input:
  • Code: INVALID_INPUT
  • HTTP Status: 400
  • Severity: LOW
  • Emoji: ❌
  • Use when: Input validation fails

Operation Failed:
  • Code: OPERATION_FAILED
  • HTTP Status: 500
  • Severity: HIGH
  • Emoji: ❌
  • Use when: Operation fails unexpectedly


INTEGRATION POINTS
==================

1. gateway/access_control.py
   - handle_access_grant_command() → create_access_denied_error()
   - handle_access_grant_command() → create_validation_error()
   - handle_access_revoke_command() → create_access_denied_error()
   - handle_access_revoke_command() → create_validation_error()
   - check_access_and_execute() → create_access_denied_error()

2. gateway/agent_commands.py
   - handle_load_agent_command() → create_access_denied_error()
   - handle_load_agent_command() → create_not_found_error()
   - handle_load_agent_command() → ErrorResponse()
   - handle_switch_instance_command() → create_access_denied_error()
   - handle_switch_instance_command() → create_validation_error()
   - handle_switch_instance_command() → create_not_found_error()


LOGGING CONSIDERATIONS
=====================

When integrating with logging, you can use the ErrorResponse fields:

  logger.error(
      f"Command failed: {error.command}",
      extra={
          "error_code": error.code,
          "severity": error.severity,
          "user_id": error.user_id,
          "context": error.context,
      }
  )

Or for monitoring:

  metrics.error_count[error.code] += 1
  metrics.severity[error.severity] += 1
  metrics.user_errors[error.user_id] += 1
