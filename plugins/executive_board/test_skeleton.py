"""
Skeleton verification tests for executive_board plugin.

These tests verify that the skeleton code structure is sound and all
required symbols are present.
"""

import pytest
import sys
from pathlib import Path

# Add plugin dir to path
PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_DIR))


def test_commands_module_imports():
    """Verify commands.py imports without errors."""
    import commands
    assert commands is not None


def test_commands_handlers_exist():
    """Verify all required command handlers are present."""
    import commands
    
    handlers = [
        'board_start',
        'board_join',
        'board_poll',
        'board_archive',
        'board_config',
        'dispatch_command',
        'init_plugin',
    ]
    
    for handler in handlers:
        assert hasattr(commands, handler), f"Missing handler: {handler}"
        assert callable(getattr(commands, handler)), f"Not callable: {handler}"


def test_commands_error_classes_exist():
    """Verify all required error classes are present."""
    import commands
    
    errors = [
        'BoardError',
        'LiveKitUnavailableError',
        'TTSFailureError',
        'DatabaseWriteTimeoutError',
        'SessionInitTimeoutError',
        'MessageRoleViolationError',
        'SchemaMutationError',
    ]
    
    for error_class in errors:
        assert hasattr(commands, error_class), f"Missing error: {error_class}"
        cls = getattr(commands, error_class)
        assert issubclass(cls, Exception), f"Not an Exception: {error_class}"


def test_voice_bridge_module_imports():
    """Verify voice_bridge.py imports without errors."""
    import voice_bridge
    assert voice_bridge is not None


def test_voice_bridge_functions_exist():
    """Verify all required voice bridge functions are present."""
    import voice_bridge
    
    functions = [
        'create_session',
        'add_participant',
        'stream_transcriptions',
        'close_session',
        'init_voice_bridge',
    ]
    
    for func in functions:
        assert hasattr(voice_bridge, func), f"Missing function: {func}"
        assert callable(getattr(voice_bridge, func)), f"Not callable: {func}"


def test_voice_bridge_error_classes_exist():
    """Verify all required voice bridge error classes are present."""
    import voice_bridge
    
    errors = [
        'VoiceBridgeException',
        'LiveKitConnectionError',
        'SessionInitTimeoutError',
        'MessageRoleViolationError',
        'SessionNotFoundError',
        'SessionExpiredError',
        'StreamingError',
    ]
    
    for error_class in errors:
        assert hasattr(voice_bridge, error_class), f"Missing error: {error_class}"
        cls = getattr(voice_bridge, error_class)
        assert issubclass(cls, Exception), f"Not an Exception: {error_class}"


def test_type_definitions_exist():
    """Verify required data classes are defined."""
    import commands
    
    types = [
        'SessionHandle',
        'DecisionSnapshot',
        'BoardSessionStatus',
    ]
    
    for type_name in types:
        assert hasattr(commands, type_name), f"Missing type: {type_name}"


def test_voice_bridge_types_exist():
    """Verify required voice bridge data classes are defined."""
    import voice_bridge
    
    types = [
        'VoiceIngress',
        'VoiceEgress',
        'TranscriptionEvent',
        'VoiceBridgeError',
    ]
    
    for type_name in types:
        assert hasattr(voice_bridge, type_name), f"Missing type: {type_name}"


def test_yaml_files_parse():
    """Verify YAML config files parse cleanly."""
    import yaml
    
    yaml_files = [
        'plugin.yaml',
        'config.example.yaml',
    ]
    
    for fname in yaml_files:
        fpath = PLUGIN_DIR / fname
        assert fpath.exists(), f"Missing file: {fname}"
        
        with open(fpath) as f:
            data = yaml.safe_load(f)
        assert data is not None, f"Failed to parse {fname}"


def test_markdown_files_exist():
    """Verify required markdown documentation exists."""
    md_files = [
        'DESIGN.md',
        'test_plan.md',
    ]
    
    for fname in md_files:
        fpath = PLUGIN_DIR / fname
        assert fpath.exists(), f"Missing file: {fname}"
        
        content = fpath.read_text()
        assert len(content) > 100, f"File too short: {fname}"


def test_hard_constraints_documented():
    """Verify hard constraints are documented in code."""
    import commands
    import voice_bridge
    
    # Check commands.py docstring mentions constraints
    assert 'Hard Constraints' in commands.__doc__
    assert 'Prompt caching sacred' in commands.__doc__
    assert 'Message role alternation' in commands.__doc__
    
    # Check voice_bridge.py docstring mentions constraints
    assert 'Hard Constraints' in voice_bridge.__doc__
    assert 'message role' in voice_bridge.__doc__.lower()


def test_error_hierarchy():
    """Verify error class hierarchy is correct."""
    import commands
    import voice_bridge
    
    # Commands errors should inherit from BoardError
    assert issubclass(commands.LiveKitUnavailableError, commands.BoardError)
    assert issubclass(commands.SessionInitTimeoutError, commands.BoardError)
    assert issubclass(commands.MessageRoleViolationError, commands.BoardError)
    
    # Voice bridge errors should inherit from VoiceBridgeException
    assert issubclass(voice_bridge.LiveKitConnectionError, voice_bridge.VoiceBridgeException)
    assert issubclass(voice_bridge.SessionInitTimeoutError, voice_bridge.VoiceBridgeException)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
