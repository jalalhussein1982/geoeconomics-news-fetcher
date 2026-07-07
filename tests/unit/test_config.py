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
    assert cfg.synthesis_model == "deepseek"
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


_ENV = {
    "DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h",
    "SMTP_USER": "u", "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x",
}


def test_rank_by_corroboration_defaults_off_and_flips():
    assert Config.from_env(dict(_ENV)).rank_by_corroboration is False
    assert Config.from_env({**_ENV, "RANK_BY_CORROBORATION": "true"}).rank_by_corroboration is True


def test_buckets_default_is_three_regions():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.buckets == ("middle_east", "north_africa", "latam")


def test_buckets_overridable_from_env():
    cfg = Config.from_env({**_ENV, "BUCKETS": "middle_east, latam"})
    assert cfg.buckets == ("middle_east", "latam")


def test_capture_overhaul_defaults():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.gate_model == "deepseek"
    assert cfg.gate_batch_size == 15
    assert cfg.region_cap == 12
    assert cfg.discovery_ceiling == 120
    assert cfg.dedup_jaccard == 0.6


def test_capture_overhaul_env_overrides():
    cfg = Config.from_env({**_ENV, "REGION_CAP": "8", "DEDUP_JACCARD": "0.7",
                           "GATE_BATCH_SIZE": "20", "DISCOVERY_CEILING": "200",
                           "GATE_MODEL": "qwen3"})
    assert cfg.region_cap == 8 and cfg.dedup_jaccard == 0.7
    assert cfg.gate_batch_size == 20 and cfg.discovery_ceiling == 200
    assert cfg.gate_model == "qwen3"


def test_embedding_and_cosine_defaults_and_overrides():
    from geoecon.config import Config
    base = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
            "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"}
    cfg = Config.from_env(base)
    assert cfg.embedding_model is None
    over = Config.from_env({**base, "EMBEDDING_MODEL": "qwen3-embedding-4b"})
    assert over.embedding_model == "qwen3-embedding-4b"


def test_dedup_model_default_and_override():
    from geoecon.config import Config
    base = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
            "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"}
    assert Config.from_env(base).dedup_model == "deepseek"
    assert Config.from_env({**base, "DEDUP_MODEL": "kimi-k2.6"}).dedup_model == "kimi-k2.6"


def test_output_quality_knob_defaults():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.figures_max == 8
    assert cfg.market_demote is True


def test_output_quality_knob_overrides():
    cfg = Config.from_env({**_ENV, "FIGURES_MAX": "5", "MARKET_DEMOTE": "false"})
    assert cfg.figures_max == 5
    assert cfg.market_demote is False


def test_curated_preference_defaults():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.curated_preference is True
    assert cfg.curated_boost == 0.15
    assert cfg.economy_section_boost == 0.10


def test_curated_preference_env_overrides():
    cfg = Config.from_env({**_ENV, "CURATED_PREFERENCE": "false",
                           "CURATED_BOOST": "0.3", "ECONOMY_SECTION_BOOST": "0.2"})
    assert cfg.curated_preference is False
    assert cfg.curated_boost == 0.3
    assert cfg.economy_section_boost == 0.2


def test_country_cap_default_and_override():
    assert Config.from_env(dict(_ENV)).country_cap == 4
    assert Config.from_env({**_ENV, "COUNTRY_CAP": "0"}).country_cap == 0
    assert Config.from_env({**_ENV, "COUNTRY_CAP": "3"}).country_cap == 3


def test_search_discovery_defaults_off():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.search_discovery is False
    assert cfg.search_country_cap == 25
    assert cfg.countries_path is None
    assert cfg.keywords_path is None
    assert cfg.gdelt_mode == "gkg"


def test_search_discovery_env_overrides():
    cfg = Config.from_env({**_ENV, "SEARCH_DISCOVERY": "true", "SEARCH_COUNTRY_CAP": "10",
                           "COUNTRIES_PATH": "/tmp/c.yaml", "KEYWORDS_PATH": "/tmp/k.yaml",
                           "GDELT_MODE": "docapi"})
    assert cfg.search_discovery is True
    assert cfg.search_country_cap == 10
    assert cfg.countries_path == "/tmp/c.yaml"
    assert cfg.keywords_path == "/tmp/k.yaml"
    assert cfg.gdelt_mode == "docapi"


def test_wave1_hardening_defaults():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.extract_workers == 12
    assert cfg.extract_timeout == 20
    assert cfg.synth_body_chars == 6000
    assert cfg.synth_total_chars == 30000


def test_wave1_hardening_env_overrides():
    cfg = Config.from_env({**_ENV, "EXTRACT_WORKERS": "4", "EXTRACT_TIMEOUT": "10",
                           "SYNTH_BODY_CHARS": "0", "SYNTH_TOTAL_CHARS": "5000"})
    assert cfg.extract_workers == 4
    assert cfg.extract_timeout == 10
    assert cfg.synth_body_chars == 0
    assert cfg.synth_total_chars == 5000
