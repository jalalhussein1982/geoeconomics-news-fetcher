from geoecon.stages import render
from geoecon.state.models import SynthesisRecord

def _rec():
    return SynthesisRecord(1, 10, {
        "headline_en": "Saudi cuts oil output",
        "what_happened_en": "Riyadh announced a 1mbd cut.",
        "geoeconomic_significance_en": "Pressures crude prices.",
        "entities": [{"canonical_en": "Saudi Arabia"}],
        "key_figures": [{"value": 1, "unit": "mbd", "gloss_en": "1 million barrels/day"}],
        "sources": [{"url": "http://x", "lang": "ar"}],
        "headline_original": "السعودية تخفض الإنتاج", "headline_original_lang": "ar",
    }, "ok", False)

def test_en_only_includes_english_excludes_original():
    archive, email = render.render_html([_rec()], mode="en_only", date_str="2026-06-19")
    assert "Saudi cuts oil output" in email and "Saudi cuts oil output" in archive
    assert "السعودية" not in email  # en_only ignores originals
    assert "<table" in email.lower()  # email is table-based

def test_zero_records_renders_heartbeat():
    archive, email = render.render_html([], mode="en_only", date_str="2026-06-19")
    assert "no qualifying" in email.lower() or "quiet" in email.lower()
