import codecs
import importlib
import os
import sys

from hermes_cli.env_loader import load_hermes_dotenv


def test_recovered_update_retry_skips_external_secret_sources(tmp_path, monkeypatch):
    """The post-recovery updater must not remap native vault dependencies."""
    import hermes_cli.env_loader as env_loader
    from hermes_cli import _early_recovery

    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("UPDATE_RETRY_DOTENV=loaded\n", encoding="utf-8")
    monkeypatch.delenv("UPDATE_RETRY_DOTENV", raising=False)
    monkeypatch.setattr(_early_recovery, "_UPDATE_RETRY_RECOVERED", True)
    external_calls = []
    monkeypatch.setattr(
        env_loader,
        "_apply_external_secret_sources",
        lambda path: external_calls.append(path),
    )

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.environ["UPDATE_RETRY_DOTENV"] == "loaded"
    assert external_calls == []


def test_utf8_bom_does_not_mangle_first_key(tmp_path, monkeypatch):
    """A leading UTF-8 BOM must not prefix the first key name in os.environ.

    PowerShell 5.1 ``Set-Content -Encoding UTF8`` and Windows Notepad write
    a BOM (EF BB BF). With encoding=utf-8, python-dotenv keeps U+FEFF on the
    first key so the canonical name is absent and callers see "not configured".
    """
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_bytes(
        b"\xef\xbb\xbfFIRST_KEY=first-value\nSECOND_KEY=second-value\n"
    )

    monkeypatch.delenv("FIRST_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)
    monkeypatch.delenv("\ufeffFIRST_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("FIRST_KEY") == "first-value"
    assert os.getenv("SECOND_KEY") == "second-value"
    assert os.environ.get("\ufeffFIRST_KEY") is None


def test_bomless_utf8_env_still_loads(tmp_path, monkeypatch):
    """BOM-less UTF-8 .env files must keep loading after utf-8-sig."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-plain\nSECOND_KEY=ok\n", encoding="utf-8")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("OPENAI_API_KEY") == "sk-plain"
    assert os.getenv("SECOND_KEY") == "ok"


def test_latin1_env_falls_back(tmp_path, monkeypatch):
    """Invalid UTF-8 bytes must still load via the latin-1 fallback."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # 0xE9 is "é" in latin-1 and not a valid UTF-8 lead sequence alone.
    env_file.write_bytes(b"LATIN1_VALUE=caf\xe9\n")

    monkeypatch.delenv("LATIN1_VALUE", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("LATIN1_VALUE") == "café"


def test_utf8_bom_preserves_first_api_key_name(tmp_path, monkeypatch):
    """Real-world case: BOM + first line is a provider API key name."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_bytes(
        b"\xef\xbb\xbfANTHROPIC_API_KEY=sk-test-123\nSECOND_KEY=ok\n"
    )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)
    monkeypatch.delenv("\ufeffANTHROPIC_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-test-123"
    assert os.getenv("SECOND_KEY") == "ok"
    assert os.environ.get("\ufeffANTHROPIC_API_KEY") is None


def test_utf8_bom_plus_invalid_utf8_preserves_first_key(tmp_path, monkeypatch):
    """BOM + non-UTF-8 body must load via latin-1 without mangling the first key.

    utf-8-sig only applies on the primary path. When invalid UTF-8 forces the
    latin-1 fallback, a leading EF BB BF would otherwise become part of the
    first key name under latin-1 and drop the canonical name.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # BOM + valid first key + latin-1 é (0xE9) in a later value.
    env_file.write_bytes(
        b"\xef\xbb\xbfANTHROPIC_API_KEY=sk-test-123\nBAD=caf\xe9\n"
    )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BAD", raising=False)
    monkeypatch.delenv("\ufeffANTHROPIC_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-test-123"
    assert os.getenv("BAD") == "café"
    assert os.environ.get("\ufeffANTHROPIC_API_KEY") is None

def test_bomless_latin1_env_still_loads(tmp_path, monkeypatch):
    """BOM-less cp1252/latin-1 .env files must keep loading after the BOM strip."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_bytes(b"LATIN1_VALUE=caf\xe9\nOTHER=ok\n")

    monkeypatch.delenv("LATIN1_VALUE", raising=False)
    monkeypatch.delenv("OTHER", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("LATIN1_VALUE") == "café"
    assert os.getenv("OTHER") == "ok"

def test_latin1_fallback_stream_honors_override(tmp_path, monkeypatch):
    """Stream-based latin-1 fallback must honor override= identically to dotenv_path."""
    from hermes_cli.env_loader import _load_dotenv_with_fallback

    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # Invalid UTF-8 forces the stream/latin-1 path.
    env_file.write_bytes(b"OVERRIDE_PROBE=from-file\nLATIN1_VALUE=caf\xe9\n")

    monkeypatch.setenv("OVERRIDE_PROBE", "from-shell")
    monkeypatch.delenv("LATIN1_VALUE", raising=False)

    # override=False: shell value must win (same as dotenv_path form).
    _load_dotenv_with_fallback(env_file, override=False)
    assert os.getenv("OVERRIDE_PROBE") == "from-shell"
    assert os.getenv("LATIN1_VALUE") == "café"

    # override=True: file value must win (user-env path).
    _load_dotenv_with_fallback(env_file, override=True)
    assert os.getenv("OVERRIDE_PROBE") == "from-file"
    assert os.getenv("LATIN1_VALUE") == "café"

def test_latin1_fallback_stream_preserves_interpolation(tmp_path, monkeypatch):
    """Stream/latin-1 path must still expand ${VAR} like the dotenv_path form."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # 0xE9 forces latin-1 fallback; ${FOO} must still expand.
    env_file.write_bytes(b"FOO=bar\nBAR=${FOO}\nLATIN1_VALUE=caf\xe9\n")

    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAR", raising=False)
    monkeypatch.delenv("LATIN1_VALUE", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("FOO") == "bar"
    assert os.getenv("BAR") == "bar"
    assert os.getenv("LATIN1_VALUE") == "café"

# ---------------------------------------------------------------------------
# UTF-16 / UTF-32 .env sanitizer coverage
#
# UTF-8 BOM handling for _load_dotenv_with_fallback is covered above (#65124).
# This section covers the sanitizer rewrite path for UTF-16/32 (and UTF-8 /
# cp1252 regression guards for that path).
# ---------------------------------------------------------------------------


def _assert_clean_utf8_env_on_disk(env_file, *, first_key: str) -> None:
    """On-disk file must be clean UTF-8: no BOM, no U+FFFD, canonical key."""
    after = env_file.read_bytes()
    assert not after.startswith(codecs.BOM_UTF8)
    assert not after.startswith(codecs.BOM_UTF16_LE)
    assert not after.startswith(codecs.BOM_UTF16_BE)
    text = after.decode("utf-8")  # strict — raises if not clean UTF-8
    assert "\ufffd" not in text
    assert text.startswith(f"{first_key}=") or f"\n{first_key}=" in text
    assert first_key.encode("ascii") in after




def test_utf16_le_bom_preserves_non_ascii_values(tmp_path, monkeypatch):
    """UTF-16-LE+BOM rewrite must preserve non-ASCII values (not just ASCII keys).

    Uses non-credential var names so _sanitize_loaded_credentials does not
    strip non-ASCII from values (that path only targets *_KEY/*_TOKEN/etc.).
    """
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    content = "GREETING=café\nCJK_LABEL=日本語\n"
    env_file.write_bytes(codecs.BOM_UTF16_LE + content.encode("utf-16-le"))

    monkeypatch.delenv("GREETING", raising=False)
    monkeypatch.delenv("CJK_LABEL", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("GREETING") == "café"
    assert os.getenv("CJK_LABEL") == "日本語"
    after = env_file.read_bytes()
    assert after.decode("utf-8")  # strict
    assert "café".encode("utf-8") in after
    assert "日本語".encode("utf-8") in after
    assert b"\xef\xbf\xbd" not in after


def test_utf32_le_bom_leaves_file_untouched(tmp_path, caplog):
    """UTF-32-LE BOM: refuse-to-mangle (leave bytes untouched + warning).

    UTF-32-LE's BOM starts with UTF-16-LE's FF FE; sniff order must check
    UTF-32 first so we never misdetect and corrupt.

    Exercises ``_sanitize_env_file_if_needed`` only: the dotenv load path
    is out of scope here (#65124's surface) and still cannot ingest UTF-32.
    """
    import logging

    from hermes_cli.env_loader import _sanitize_env_file_if_needed

    env_file = tmp_path / ".env"
    content = "HERMES_TEST_KEY=hello_utf32\nSECOND_KEY=world\n"
    raw = codecs.BOM_UTF32_LE + content.encode("utf-32-le")
    env_file.write_bytes(raw)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.env_loader"):
        _sanitize_env_file_if_needed(env_file)

    assert env_file.read_bytes() == raw  # untouched
    assert any("UTF-32" in r.message for r in caplog.records)




def test_utf32_warning_fires_once_per_path(tmp_path, caplog, monkeypatch):
    """Three sanitize calls on the same UTF-32 file → exactly one warning.

    Matches house style for warn-once (module-level seen-set, same class as
    ``_WARNED_KEYS``): hot-reload / multi-entry load must not spam logs.
    """
    import logging

    import hermes_cli.env_loader as env_loader
    from hermes_cli.env_loader import _sanitize_env_file_if_needed

    # Isolate process-level seen-set so other tests' paths don't leak in.
    monkeypatch.setattr(env_loader, "_WARNED_UTF32_PATHS", set())

    env_file = tmp_path / ".env"
    content = "HERMES_TEST_KEY=hello_utf32\nSECOND_KEY=world\n"
    raw = codecs.BOM_UTF32_LE + content.encode("utf-32-le")
    env_file.write_bytes(raw)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.env_loader"):
        _sanitize_env_file_if_needed(env_file)
        _sanitize_env_file_if_needed(env_file)
        _sanitize_env_file_if_needed(env_file)

    utf32_warnings = [r for r in caplog.records if "UTF-32" in r.message]
    assert len(utf32_warnings) == 1
    assert env_file.read_bytes() == raw




def test_plain_utf8_env_regression(tmp_path, monkeypatch):
    """Plain UTF-8 .env must keep loading after the UTF-16 sanitize changes."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    before = b"OPENAI_API_KEY=sk-plain\nSECOND_KEY=ok\n"
    env_file.write_bytes(before)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("OPENAI_API_KEY") == "sk-plain"
    assert os.getenv("SECOND_KEY") == "ok"
    # No spurious rewrite of an already-clean file.
    assert env_file.read_bytes() == before


def test_cp1252_env_regression_does_not_crash(tmp_path, monkeypatch):
    """cp1252/latin-1 body must not crash sanitize; ASCII keys still usable.

    0xE9 is 'é' in cp1252 and incomplete as UTF-8. First line does not begin
    with U+FFFD, so the FFFD guard must not refuse the whole file.

    Sanitize leaves the file bytes alone when the only "change" is
    errors=replace on values (original already replace-decoded equals
    sanitized), so _load_dotenv_with_fallback's latin-1 path recovers café.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    before = b"ASCII_KEY=ok\nLATIN1_VALUE=caf\xe9\n"
    env_file.write_bytes(before)

    monkeypatch.delenv("ASCII_KEY", raising=False)
    monkeypatch.delenv("LATIN1_VALUE", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("ASCII_KEY") == "ok"
    assert os.getenv("LATIN1_VALUE") == "café"
    # Sanitize must not have rewritten (would have persisted U+FFFD).
    assert env_file.read_bytes() == before


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
