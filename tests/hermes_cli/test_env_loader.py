import importlib
import os
import sys
from pathlib import Path

from hermes_cli.env_loader import load_hermes_dotenv


def test_user_env_overrides_stale_shell_values(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("OPENAI_BASE_URL=https://new.example/v1\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("OPENAI_BASE_URL") == "https://new.example/v1"


def test_project_env_overrides_stale_shell_values_when_user_env_missing(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    project_env = tmp_path / ".env"
    project_env.write_text("OPENAI_BASE_URL=https://project.example/v1\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [project_env]
    assert os.getenv("OPENAI_BASE_URL") == "https://project.example/v1"


def test_project_env_is_sanitized_before_loading(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    project_env = tmp_path / ".env"
    project_env.write_text(
        "TELEGRAM_BOT_TOKEN=0123456789:test"
        "ANTHROPIC_API_KEY=sk-ant-test123\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [project_env]
    assert os.getenv("TELEGRAM_BOT_TOKEN") == "0123456789:test"
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-ant-test123"


def test_user_env_takes_precedence_over_project_env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    user_env = home / ".env"
    project_env = tmp_path / ".env"
    user_env.write_text("OPENAI_BASE_URL=https://user.example/v1\n", encoding="utf-8")
    project_env.write_text("OPENAI_BASE_URL=https://project.example/v1\nOPENAI_API_KEY=project-key\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [user_env, project_env]
    assert os.getenv("OPENAI_BASE_URL") == "https://user.example/v1"
    assert os.getenv("OPENAI_API_KEY") == "project-key"


def test_voice_twins_env_fills_missing_without_overriding(tmp_path, monkeypatch):
    """ADR-013: ~/.env.voice_twins is loaded as a no-override third tier.

    Voice-twin credentials (LIVEKIT_*, RESEMBLE_*, DEEPGRAM_*) defined in
    that file should fill missing keys without clobbering values already
    set by ~/.hermes/.env (which remains authoritative).
    """
    home = tmp_path / "hermes"
    home.mkdir()
    user_env = home / ".env"
    user_env.write_text("LIVEKIT_URL=wss://user.example\n", encoding="utf-8")

    voice_twins = tmp_path / ".env.voice_twins"
    voice_twins.write_text(
        "LIVEKIT_URL=wss://voicetwins.example\n"  # should NOT override user_env
        "LIVEKIT_API_KEY=voicetwins-key\n"          # should fill missing
        "LIVEKIT_API_SECRET=voicetwins-secret\n",   # should fill missing
        encoding="utf-8",
    )

    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [user_env, voice_twins]
    # User env wins for the conflicting key.
    assert os.getenv("LIVEKIT_URL") == "wss://user.example"
    # Voice-twins fills missing keys.
    assert os.getenv("LIVEKIT_API_KEY") == "voicetwins-key"
    assert os.getenv("LIVEKIT_API_SECRET") == "voicetwins-secret"


def test_voice_twins_env_absent_is_silent(tmp_path):
    """Loader must not error or include voice_twins path when file is missing."""
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text("FOO=bar\n", encoding="utf-8")

    loaded = load_hermes_dotenv(hermes_home=home)

    # No voice-twins file in tmp_path → only the user .env loaded.
    assert loaded == [home / ".env"]


def test_main_import_applies_user_env_over_shell_values(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "OPENAI_BASE_URL=https://new.example/v1\nHERMES_INFERENCE_PROVIDER=custom\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")

    sys.modules.pop("hermes_cli.main", None)
    importlib.import_module("hermes_cli.main")

    assert os.getenv("OPENAI_BASE_URL") == "https://new.example/v1"
    assert os.getenv("HERMES_INFERENCE_PROVIDER") == "custom"
