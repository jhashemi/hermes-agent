"""Skeleton verification tests for executive_board plugin."""

import pytest
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent.parent / 'plugins' / 'executive_board'
sys.path.insert(0, str(PLUGIN_DIR))


def test_commands_module_imports():
    """Verify commands.py imports without errors."""
    import commands
    assert commands is not None


def test_commands_handlers_exist():
    """Verify all required command handlers are present."""
    import commands
    handlers = [
        'board_start', 'board_join', 'board_poll', 'board_archive', 
        'board_config', 'dispatch_command', 'init_plugin',
    ]
    for handler in handlers:
        assert hasattr(commands, handler)
        assert callable(getattr(commands, handler))


def test_commands_error_classes_exist():
    """Verify all required error classes are present."""
    import commands
    errors = [
        'BoardError', 'LiveKitUnavailableError', 'TTSFailureError',
        'DatabaseWriteTimeoutError', 'SessionInitTimeoutError',
        'MessageRoleViolationError', 'SchemaMutationError',
    ]
    for error_class in errors:
        assert hasattr(commands, error_class)
        cls = getattr(commands, error_class)
        assert issubclass(cls, Exception)


def test_voice_bridge_module_imports():
    """Verify voice_bridge.py imports without errors."""
    import voice_bridge
    assert voice_bridge is not None


def test_voice_bridge_functions_exist():
    """Verify all required voice bridge functions are present."""
    import voice_bridge
    functions = [
        'create_session', 'add_participant', 'stream_transcriptions',
        'close_session', 'init_voice_bridge',
    ]
    for func in functions:
        assert hasattr(voice_bridge, func)
        assert callable(getattr(voice_bridge, func))


def test_voice_bridge_error_classes_exist():
    """Verify all required voice bridge error classes are present."""
    import voice_bridge
    errors = [
        'VoiceBridgeException', 'LiveKitConnectionError',
        'SessionInitTimeoutError', 'MessageRoleViolationError',
        'SessionNotFoundError', 'SessionExpiredError', 'StreamingError',
    ]
    for error_class in errors:
        assert hasattr(voice_bridge, error_class)
        cls = getattr(voice_bridge, error_class)
        assert issubclass(cls, Exception)


def test_type_definitions_exist():
    """Verify required data classes are defined."""
    import commands
    types = ['SessionHandle', 'DecisionSnapshot', 'BoardSessionStatus']
    for type_name in types:
        assert hasattr(commands, type_name)


def test_voice_bridge_types_exist():
    """Verify required voice bridge data classes are defined."""
    import voice_bridge
    types = ['VoiceIngress', 'VoiceEgress', 'TranscriptionEvent', 'VoiceBridgeError']
    for type_name in types:
        assert hasattr(voice_bridge, type_name)


def test_yaml_files_parse():
    """Verify YAML config files parse cleanly."""
    import yaml
    for fname in ['plugin.yaml', 'config.example.yaml']:
        fpath = PLUGIN_DIR / fname
        assert fpath.exists()
        with open(fpath) as f:
            data = yaml.safe_load(f)
        assert data is not None


def test_markdown_files_exist():
    """Verify required markdown documentation exists."""
    for fname in ['DESIGN.md', 'test_plan.md']:
        fpath = PLUGIN_DIR / fname
        assert fpath.exists()
        content = fpath.read_text()
        assert len(content) > 100


def test_hard_constraints_documented():
    """Verify hard constraints are documented in code."""
    import commands, voice_bridge
    assert 'Hard Constraints' in commands.__doc__
    assert 'Prompt caching sacred' in commands.__doc__
    assert 'Hard Constraints' in voice_bridge.__doc__


def test_error_hierarchy():
    """Verify error class hierarchy is correct."""
    import commands, voice_bridge
    assert issubclass(commands.LiveKitUnavailableError, commands.BoardError)
    assert issubclass(commands.SessionInitTimeoutError, commands.BoardError)
    assert issubclass(voice_bridge.LiveKitConnectionError, voice_bridge.VoiceBridgeException)
