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
    synthesis_model: str = "kimi-k2.6"
    synthesis_prompt_id: str = "task_a_cov"
    embedding_model: str | None = None
    regions: tuple[str, ...] = ("MENA", "LatAm")
    buckets: tuple[str, ...] = BUCKET_ORDER
    gate_model: str = "kimi-k2.6"
    dedup_model: str = "kimi-k2.6"
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
            synthesis_model=env.get("SYNTHESIS_MODEL", "kimi-k2.6"),
            synthesis_prompt_id=env.get("SYNTHESIS_PROMPT_ID", "task_a_cov"),
            embedding_model=env.get("EMBEDDING_MODEL") or None,
            regions=_split(env.get("REGIONS", "MENA,LatAm")),
            buckets=_split(env.get("BUCKETS", ",".join(BUCKET_ORDER))),
            gate_model=env.get("GATE_MODEL", "kimi-k2.6"),
            dedup_model=env.get("DEDUP_MODEL", "kimi-k2.6"),
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
            limit=int(limit) if limit else None,
        )
