"""Regression tests for OKR-7: In-session structured parameter persistence.

Verifies that the todo tool correctly serves as an in-session context store
for structured parameters (location, strategy, financial targets, etc.) and
that they survive context compression via format_for_injection.

Failure mode this tests against:
  User states: Newport Beach, rental hold strategy, 17% IRR target.
  Agent responds (later in same session): "I'm not finding it in the system."
  Root cause: agent never wrote params to todo store; relied only on
  conversation context which got compressed away.
"""
import json

import pytest

from tools.todo_tool import TodoStore, TODO_INJECTION_HEADER, todo_tool


class TestSessionParamsInTodoStore:
    """Verify that structured session parameters can be stored and retrieved."""

    def test_session_params_stored_and_retrieved(self):
        """KR1: structured params written to todo store are retrievable."""
        store = TodoStore()
        params_content = (
            "Location: Newport Beach CA | "
            "Strategy: rental hold | "
            "IRR target: 17% | "
            "Market: Orange County"
        )
        store.write([{
            "id": "session_params",
            "content": params_content,
            "status": "in_progress",
        }])

        items = store.read()
        assert len(items) == 1
        assert items[0]["id"] == "session_params"
        assert "Newport Beach" in items[0]["content"]
        assert "17%" in items[0]["content"]
        assert "rental hold" in items[0]["content"]
        assert items[0]["status"] == "in_progress"

    def test_session_params_survive_format_for_injection(self):
        """KR3: session params in todo store survive context compression injection."""
        store = TodoStore()
        store.write([{
            "id": "real_estate_params",
            "content": "Location: Newport Beach CA | Strategy: rental hold | IRR: 17%",
            "status": "in_progress",
        }])

        injected = store.format_for_injection()
        assert injected is not None
        assert "Newport Beach" in injected
        assert "17%" in injected
        assert TODO_INJECTION_HEADER in injected

    def test_session_params_survive_mixed_todo_list(self):
        """Params should survive alongside real task items."""
        store = TodoStore()
        store.write([
            {
                "id": "real_estate_params",
                "content": "Location: Newport Beach CA | IRR: 17% | Strategy: rental hold",
                "status": "in_progress",
            },
            {
                "id": "task_1",
                "content": "Run IRR analysis for OC market",
                "status": "pending",
            },
            {
                "id": "task_2",
                "content": "Fetch comparable sales data",
                "status": "pending",
            },
        ])

        injected = store.format_for_injection()
        assert injected is not None
        assert "Newport Beach" in injected
        assert "17%" in injected
        assert "IRR analysis" in injected

    def test_completed_session_params_not_re_injected(self):
        """Completed/cancelled params should not re-inject (avoid confusion)."""
        store = TodoStore()
        store.write([{
            "id": "session_params",
            "content": "Location: Newport Beach CA | IRR: 17%",
            "status": "completed",  # session goal achieved, mark done
        }])

        injected = store.format_for_injection()
        # Completed items are filtered from injection
        assert injected is None

    def test_session_params_updated_in_place_via_merge(self):
        """KR3: params can be updated as the session evolves."""
        store = TodoStore()
        # Initial params
        store.write([{
            "id": "session_params",
            "content": "Location: Newport Beach CA | IRR: 17%",
            "status": "in_progress",
        }])
        # User adds timeframe → update in place
        store.write([{
            "id": "session_params",
            "content": "Location: Newport Beach CA | IRR: 17% | Timeframe: 5 years",
            "status": "in_progress",
        }], merge=True)

        items = store.read()
        assert len(items) == 1
        assert "5 years" in items[0]["content"]
        assert "Newport Beach" in items[0]["content"]

    def test_params_injected_after_compression(self):
        """Simulate post-compression re-injection path.

        The ContextCompressor calls format_for_injection() and appends the
        result to the compressed history. This test verifies that the injected
        text is readable and contains the key parameters.
        """
        store = TodoStore()
        store.write([{
            "id": "deal_params",
            "content": (
                "Location: Newport Beach CA | "
                "Deal type: SFR | "
                "Hold strategy: rental | "
                "Target IRR: 17% | "
                "Market: Orange County | "
                "Max purchase: $2.1M"
            ),
            "status": "in_progress",
        }])

        injected = store.format_for_injection()
        assert injected is not None

        # All key params visible in the injected block
        for expected in ["Newport Beach", "17%", "rental", "Orange County", "$2.1M"]:
            assert expected in injected, f"Expected '{expected}' in injected block"

    def test_todo_tool_function_stores_params(self):
        """End-to-end: calling todo_tool() stores and returns session params."""
        store = TodoStore()
        result_json = todo_tool(
            todos=[{
                "id": "session_params",
                "content": "Location: Newport Beach CA | IRR: 17% | Strategy: rental hold",
                "status": "in_progress",
            }],
            store=store,
        )
        result = json.loads(result_json)
        assert result["todos"][0]["id"] == "session_params"
        assert "17%" in result["todos"][0]["content"]
        assert result["summary"]["in_progress"] == 1

    def test_no_session_params_no_false_injection(self):
        """Empty store does not inject anything (no phantom context)."""
        store = TodoStore()
        assert store.format_for_injection() is None


class TestSessionParamsGuidanceStrings:
    """Verify guidance strings reference in-session parameter storage."""

    def test_todo_schema_mentions_session_params(self):
        """The todo tool description must explain the session parameter use case."""
        from tools.todo_tool import TODO_SCHEMA
        desc = TODO_SCHEMA["description"]
        # Must mention survival across context compression
        assert "context compression" in desc.lower()
        # Must mention structured parameters
        assert "session_params" in desc or "structured parameter" in desc.lower()
        # Must mention IRR or financial target as example
        assert "IRR" in desc or "financial target" in desc.lower()

    def test_memory_guidance_mentions_todo_for_session_params(self):
        """MEMORY_GUIDANCE must direct agents to use todo for in-session params."""
        from agent.prompt_builder import MEMORY_GUIDANCE
        # Must explicitly mention todo
        assert "todo" in MEMORY_GUIDANCE.lower()
        # Must mention in-session params
        assert "session" in MEMORY_GUIDANCE.lower()
        # Must mention structured params (location/target/strategy)
        assert "location" in MEMORY_GUIDANCE.lower() or "strategy" in MEMORY_GUIDANCE.lower()

    def test_session_search_guidance_warns_about_current_session(self):
        """SESSION_SEARCH_GUIDANCE must clarify it only searches past sessions."""
        from agent.prompt_builder import SESSION_SEARCH_GUIDANCE
        # Must state it covers past sessions only
        guidance_lower = SESSION_SEARCH_GUIDANCE.lower()
        assert "past" in guidance_lower or "previous" in guidance_lower
        # Must NOT imply it covers the current session
        # (it should warn: "not the current live session")
        assert "current" in guidance_lower
        # Must give positive direction for current-session data
        assert "todo" in guidance_lower

    def test_session_search_guidance_no_finding_it_failure(self):
        """Guidance must explicitly address the 'I'm not finding it' failure."""
        from agent.prompt_builder import SESSION_SEARCH_GUIDANCE
        # Must warn against saying "I'm not finding it"
        assert "not finding it" in SESSION_SEARCH_GUIDANCE or "not finding" in SESSION_SEARCH_GUIDANCE
