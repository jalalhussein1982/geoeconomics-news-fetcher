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
    synthesis_model: str = "deepseek-v3.2"
    synthesis_prompt_id: str = "task_a_cov"
    embedding_model: str | None = None
    regions: tuple[str, ...] = ("MENA", "LatAm")
    buckets: tuple[str, ...] = BUCKET_ORDER
    window_hours: int = 24
    timezone: str = "Europe/Prague"
    archive_dir: str = "/data/archive"
    send_heartbeat: bool = True
    dry_run: bool = False
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
            synthesis_model=env.get("SYNTHESIS_MODEL", "deepseek-v3.2"),
            synthesis_prompt_id=env.get("SYNTHESIS_PROMPT_ID", "task_a_cov"),
            embedding_model=env.get("EMBEDDING_MODEL") or None,
            regions=_split(env.get("REGIONS", "MENA,LatAm")),
            buckets=_split(env.get("BUCKETS", ",".join(BUCKET_ORDER))),
            window_hours=int(env.get("WINDOW_HOURS", "24")),
            timezone=env.get("TIMEZONE", "Europe/Prague"),
            archive_dir=env.get("ARCHIVE_DIR", "/data/archive"),
            send_heartbeat=env.get("SEND_HEARTBEAT", "true").lower() == "true",
            dry_run=env.get("DRY_RUN", "false").lower() == "true",
            limit=int(limit) if limit else None,
        )
