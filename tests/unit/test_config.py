from geoecon.config import Config

BASE_ENV = {
    "DB_DSN": "postgresql://u:p@h:5432/geoeconomics",
    "MAETCENTRUM_KEY": "sk-test",
    "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587",
    "SMTP_USER": "me@gmail.com", "SMTP_PASSWORD": "pw",
    "FROM_ADDR": "me@gmail.com", "RECIPIENTS": "a@x.com, b@y.com",
}

def test_from_env_defaults():
    cfg = Config.from_env(BASE_ENV)
    assert cfg.synthesis_model == "deepseek-v3.2"
    assert cfg.synthesis_prompt_id == "task_a_cov"
    assert cfg.llm_base_url == "https://llm.ai.e-infra.cz/v1"
    assert cfg.window_hours == 24
    assert cfg.regions == ("MENA", "LatAm")
    assert cfg.recipients == ("a@x.com", "b@y.com")
    assert cfg.timezone == "Europe/Prague"
    assert cfg.dry_run is False and cfg.limit is None

def test_api_key_fallback():
    env = {k: v for k, v in BASE_ENV.items() if k != "MAETCENTRUM_KEY"}
    env["METACENTRUM_KEY"] = "sk-fallback"
    assert Config.from_env(env).llm_api_key == "sk-fallback"

def test_missing_required_raises():
    import pytest
    with pytest.raises(KeyError):
        Config.from_env({})
