"""Coverage matrix: countries x languages x keyword families (spec 2026-07-04).

`config/countries.yaml` is THE coverage definition; `config/keywords.yaml` is
the two-family query vocabulary. Both are data, editable without code changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# Google News interface language per keyword language. LATAM editions use the
# regional es-419 / pt-419 forms (validated in the 2026-07-04 probe).
_HL = {"ar": "ar", "es": "es-419", "pt": "pt-BR", "tr": "tr", "he": "he",
       "fr": "fr", "en": "en"}


@dataclass(frozen=True)
class CountrySpec:
    fips: str
    name: str
    gl: str
    langs: tuple[str, ...]
    gdelt_sc: str
    global_en: bool = False
    ceid: dict[str, str] = field(default_factory=dict)


def load_countries(path: str | None = None) -> list[CountrySpec]:
    p = Path(path) if path else _CONFIG_DIR / "countries.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    out = []
    for row in data["countries"]:
        out.append(CountrySpec(
            fips=row["fips"], name=row["name"], gl=row["gl"],
            langs=tuple(row.get("langs") or ()),
            gdelt_sc=row["gdelt_sc"],
            global_en=bool(row.get("global_en", False)),
            ceid=dict(row.get("ceid") or {}),
        ))
    return out


def load_keywords(path: str | None = None) -> dict[str, dict[str, list[str]]]:
    p = Path(path) if path else _CONFIG_DIR / "keywords.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def or_expr(terms: list[str]) -> str:
    return "(" + " OR ".join(terms) + ")"


def hl_for(lang: str) -> str:
    return _HL[lang]


def ceid_for(spec: CountrySpec, lang: str) -> str:
    return spec.ceid.get(lang) or f"{spec.gl}:{hl_for(lang)}"
