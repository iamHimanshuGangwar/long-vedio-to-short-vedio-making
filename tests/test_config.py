import pytest

from app import config

KEYS = ("AI_PROVIDER", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@pytest.fixture
def clean_env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "OLLAMA_EXE", "")
    return monkeypatch


def test_no_configuration_means_basic(clean_env):
    assert config.ai_provider() is None


def test_forced_provider_wins(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "x")
    clean_env.setenv("AI_PROVIDER", "ollama")
    assert config.ai_provider() == "ollama"


def test_forced_none(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "x")
    clean_env.setenv("AI_PROVIDER", "none")
    assert config.ai_provider() is None


def test_gemini_preferred_over_claude(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "x")
    clean_env.setenv("ANTHROPIC_API_KEY", "y")
    assert config.ai_provider() == "gemini"


def test_ollama_detected_from_executable(clean_env, tmp_path):
    exe = tmp_path / "ollama.exe"
    exe.write_text("")
    clean_env.setattr(config, "OLLAMA_EXE", str(exe))
    assert config.ai_provider() == "ollama"
