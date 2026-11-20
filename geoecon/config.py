from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping

from geoecon.regions import BUCKET_ORDER

def _split(csv: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in csv.split(",") if x.strip())

@dataclass(frozen=True)
class Config:
    db_dsn: str
    llm_api_key: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    from_addr: str
    recipients: tuple[str, ...]
    llm_base_url: str = "https://llm.ai.e-infra.cz/v1"
    synthesis_model: str = "deepseek"
    synthesis_prompt_id: str = "task_a_cov"
    embedding_model: str | None = None
    regions: tuple[str, ...] = ("MENA", "LatAm")
    buckets: tuple[str, ...] = BUCKET_ORDER
    gate_model: str = "deepseek"
    dedup_model: str = "deepseek"
    gate_batch_size: int = 15
    region_cap: int = 12
    discovery_ceiling: int = 120
    dedup_jaccard: float = 0.6
    window_hours: int = 24
    timezone: str = "Europe/Prague"
    archive_dir: str = "/data/archive"
    sources_path: str | None = None  # registry path; None => registry.default_path()
    send_heartbeat: bool = True
    dry_run: bool = False
    # Change 4: rank summary/render by corroboration_score instead of gate_score.
    # Ships OFF — flip only after reviewing a real before/after brief diff in a live env.
    rank_by_corroboration: bool = False
    # Output-quality knobs (2026-06-23 refinement). Both reversible via env.
    figures_max: int = 8          # FIGURES_MAX; cap per-story figures line (0 => no cap)
    market_demote: bool = True    # MARKET_DEMOTE; rank US/global market roundups last
    # Curated-source ranking nudge (Unit B). Reversible via env; master flag default ON.
    curated_preference: bool = True       # CURATED_PREFERENCE
    curated_boost: float = 0.15           # CURATED_BOOST
    economy_section_boost: float = 0.10   # ECONOMY_SECTION_BOOST
    country_cap: int = 4                   # COUNTRY_CAP; max records per country per region (0 disables)
    # Discovery redesign (spec 2026-07-04). All default-off / inert until Phase 4 flips env.
    search_discovery: bool = False        # SEARCH_DISCOVERY; enables discovery_search stage
    search_country_cap: int = 25          # SEARCH_COUNTRY_CAP; max inserts per provider+country+family feed
    countries_path: str | None = None     # COUNTRIES_PATH; None => config/countries.yaml
    keywords_path: str | None = None      # KEYWORDS_PATH; None => config/keywords.yaml
    gdelt_mode: str = "gkg"               # GDELT_MODE: gkg (legacy files) | docapi (DOC 2.0)
    # Wave-1 hardening (2026-07-04): throughput + input-bounding knobs.
    extract_workers: int = 12         # EXTRACT_WORKERS; thread-pool width for body fetches
    extract_timeout: int = 20         # EXTRACT_TIMEOUT; per-fetch download timeout (s)
    synth_body_chars: int = 6000      # SYNTH_BODY_CHARS; per-article body cap into synthesis (0 = off)
    synth_total_chars: int = 30000    # SYNTH_TOTAL_CHARS; per-cluster input cap (0 = off)
    # Per-stage LLM output-token budgets. Reasoning models (e.g. kimi-k2.6) spend
    # part of this budget on hidden reasoning tokens BEFORE any answer text, so a
    # cap that is too small yields empty/truncated content (finish_reason=length)
    # -> gate "unparsed", event-grouping failure, stalled synthesis. Raised and made
    # env-tunable so the model can change without a rebuild. All reversible via env.
    # LLM-call concurrency (2026-07-09): gate+synthesis were ~3h of SEQUENTIAL
    # calls — the 4h deadline-kill incident. Threads do only the LLM round-trip;
    # all DB writes stay on the main thread (psycopg conn is not thread-safe).
    # Set to 1 to restore byte-identical sequential behavior without a rebuild.
    gate_workers: int = 4             # GATE_WORKERS; concurrent gate batches
    synth_workers: int = 4            # SYNTH_WORKERS; concurrent cluster syntheses
    # Consecutive fully-exhausted LLM calls before the run is failed as a systemic
    # outage rather than ground on until the deadline (run 67 burned 6h50m that
    # way). Set to 0 to disable the breaker without a rebuild.
    llm_systemic_threshold: int = 5    # LLM_SYSTEMIC_THRESHOLD
    gate_max_tokens: int = 8192       # GATE_MAX_TOKENS
    dedup_max_tokens: int = 8192      # DEDUP_MAX_TOKENS (per-country event grouping)
    synth_max_tokens: int = 16384     # SYNTH_MAX_TOKENS (per-cluster record)
    summary_max_tokens: int = 8192    # SUMMARY_MAX_TOKENS (per-bucket lede/takeaways)
    limit: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        api_key = env.get("MAETCENTRUM_KEY") or env.get("METACENTRUM_KEY")
        if not api_key:
            raise KeyError("MAETCENTRUM_KEY (or METACENTRUM_KEY) is required")
        limit = env.get("LIMIT")
        return cls(
            db_dsn=env["DB_DSN"],
            llm_api_key=api_key,
            smtp_host=env["SMTP_HOST"],
            smtp_port=int(env.get("SMTP_PORT", "587")),
            smtp_user=env["SMTP_USER"],
            smtp_password=env["SMTP_PASSWORD"],
            from_addr=env["FROM_ADDR"],
            recipients=_split(env["RECIPIENTS"]),
            llm_base_url=env.get("LLM_BASE_URL", "https://llm.ai.e-infra.cz/v1"),
            synthesis_model=env.get("SYNTHESIS_MODEL", "deepseek"),
            synthesis_prompt_id=env.get("SYNTHESIS_PROMPT_ID", "task_a_cov"),
            embedding_model=env.get("EMBEDDING_MODEL") or None,
            regions=_split(env.get("REGIONS", "MENA,LatAm")),
            buckets=_split(env.get("BUCKETS", ",".join(BUCKET_ORDER))),
            gate_model=env.get("GATE_MODEL", "deepseek"),
            dedup_model=env.get("DEDUP_MODEL", "deepseek"),
            gate_batch_size=int(env.get("GATE_BATCH_SIZE", "15")),
            region_cap=int(env.get("REGION_CAP", "12")),
            discovery_ceiling=int(env.get("DISCOVERY_CEILING", "120")),
            dedup_jaccard=float(env.get("DEDUP_JACCARD", "0.6")),
            window_hours=int(env.get("WINDOW_HOURS", "24")),
            timezone=env.get("TIMEZONE", "Europe/Prague"),
            archive_dir=env.get("ARCHIVE_DIR", "/data/archive"),
            sources_path=env.get("SOURCES_PATH") or None,
            send_heartbeat=env.get("SEND_HEARTBEAT", "true").lower() == "true",
            dry_run=env.get("DRY_RUN", "false").lower() == "true",
            rank_by_corroboration=env.get("RANK_BY_CORROBORATION", "false").lower() == "true",
            figures_max=int(env.get("FIGURES_MAX", "8")),
            market_demote=env.get("MARKET_DEMOTE", "true").lower() == "true",
            curated_preference=env.get("CURATED_PREFERENCE", "true").lower() == "true",
            curated_boost=float(env.get("CURATED_BOOST", "0.15")),
            economy_section_boost=float(env.get("ECONOMY_SECTION_BOOST", "0.10")),
            country_cap=int(env.get("COUNTRY_CAP", "4")),
            search_discovery=env.get("SEARCH_DISCOVERY", "false").lower() == "true",
            search_country_cap=int(env.get("SEARCH_COUNTRY_CAP", "25")),
            countries_path=env.get("COUNTRIES_PATH") or None,
            keywords_path=env.get("KEYWORDS_PATH") or None,
            gdelt_mode=env.get("GDELT_MODE", "gkg"),
            extract_workers=int(env.get("EXTRACT_WORKERS", "12")),
            extract_timeout=int(env.get("EXTRACT_TIMEOUT", "20")),
            synth_body_chars=int(env.get("SYNTH_BODY_CHARS", "6000")),
            synth_total_chars=int(env.get("SYNTH_TOTAL_CHARS", "30000")),
            gate_workers=int(env.get("GATE_WORKERS", "4")),
            synth_workers=int(env.get("SYNTH_WORKERS", "4")),
            llm_systemic_threshold=int(env.get("LLM_SYSTEMIC_THRESHOLD", "5")),
            gate_max_tokens=int(env.get("GATE_MAX_TOKENS", "8192")),
            dedup_max_tokens=int(env.get("DEDUP_MAX_TOKENS", "8192")),
            synth_max_tokens=int(env.get("SYNTH_MAX_TOKENS", "16384")),
            summary_max_tokens=int(env.get("SUMMARY_MAX_TOKENS", "8192")),
            limit=int(limit) if limit else None,
        )
