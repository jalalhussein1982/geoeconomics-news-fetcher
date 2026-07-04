"""One-shot: retire tier-1/2 generic outlets (spec 2026-07-04 registry trim).
Search discovery covers these outlets now; tier-0 primaries and actor_* stay.
Run: .venv/bin/python tools/retire_generic_outlets.py"""
from pathlib import Path

from ruamel.yaml import YAML

path = Path(__file__).resolve().parents[1] / "config" / "sources.yaml"
yaml = YAML()
yaml.preserve_quotes = True
data = yaml.load(path.read_text(encoding="utf-8"))

changed = []
for src in data["sources"]:
    if src.get("tier") in (1, 2) and not str(src.get("id", "")).startswith("actor_"):
        if src.get("status") != "retired":
            src["status"] = "retired"
            changed.append(src["id"])

yaml.dump(data, path.open("w", encoding="utf-8"))
print(f"retired {len(changed)}: {', '.join(changed)}")
