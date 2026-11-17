"""Credential-profile routing: detection, selection, switching (no secrets)."""
from unittest.mock import patch

from urai_router.auth_routes import AuthRouter, default_profiles


def test_default_profiles_cover_services():
    names = {p.name for p in default_profiles()}
    assert {
        "colab-oauth", "colab-adc", "kaggle-file", "kaggle-env",
        "dashscope", "ark", "urai-api", "llm-keys",
    } <= names
    assert {p.service for p in default_profiles()} >= {"colab", "kaggle", "media", "urai", "llm"}


def test_detect_requires_env_and_does_not_echo_values(tmp_path):
    router = AuthRouter(state_path=tmp_path / "state.json")
    with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "secret-value-must-not-appear"}):
        row = next(r for r in router.detect() if r["name"] == "dashscope")
        assert row["available"] is True
        assert "secret-value-must-not-appear" not in str(row)
        assert row["requires"]["env"] == ["DASHSCOPE_API_KEY"]


def test_detect_reports_missing(tmp_path):
    router = AuthRouter(state_path=tmp_path / "state.json")
    with patch.dict("os.environ", {"DASHSCOPE_API_KEY": ""}):
        row = next(r for r in router.detect() if r["name"] == "dashscope")
        assert row["available"] is False
        assert "env:DASHSCOPE_API_KEY" in row["missing"]


def test_select_prefers_explicit_then_priority(tmp_path):
    router = AuthRouter(state_path=tmp_path / "state.json")
    with patch.dict("os.environ", {"KAGGLE_USERNAME": "u", "KAGGLE_KEY": "k"}):
        chosen = router.select("kaggle")
        assert chosen is not None and chosen.name == "kaggle-env"
        # A preferred profile that is not available must not be forced.
        assert router.select("kaggle", preferred="kaggle-file") is None


def test_switch_pins_active_and_status(tmp_path):
    router = AuthRouter(state_path=tmp_path / "state.json")
    with patch.dict("os.environ", {"KAGGLE_USERNAME": "u", "KAGGLE_KEY": "k"}):
        result = router.switch("kaggle-env")
        assert result["ok"] is True
        assert router.active("kaggle") == "kaggle-env"
        assert tmp_path.joinpath("state.json").exists()
        status = router.status()
        assert status["services"]["kaggle"]["active"] == "kaggle-env"
        assert status["services"]["kaggle"]["available"] >= 1
