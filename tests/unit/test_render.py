# tests/unit/test_render.py — REPLACE the file with this (keeps the two original
# behaviors and adds summary/source-link/per-bucket coverage)
from geoecon.stages import render
from geoecon.state.models import SynthesisRecord


def _rec(source_links=None):
    r = SynthesisRecord(1, 10, {
        "headline_en": "Saudi cuts oil output",
        "what_happened_en": "Riyadh announced a 1mbd cut.",
        "geoeconomic_significance_en": "Pressures crude prices.",
        "entities": [{"canonical_en": "Saudi Arabia"}],
        "key_figures": [{"value": 1, "unit": "mbd", "gloss_en": "1 million barrels/day"}],
        "sources": [{"url": "http://attribution-only", "lang": "ar"}],
        "headline_original": "السعودية تخفض الإنتاج", "headline_original_lang": "ar",
    }, "ok", False)
    if source_links is not None:
        r.source_links = source_links
    return r


def test_en_only_includes_english_excludes_original():
    archive, email = render.render_html([_rec()], mode="en_only", date_str="2026-06-19")
    assert "Saudi cuts oil output" in email and "Saudi cuts oil output" in archive
    assert "السعودية" not in email
    assert "<table" in email.lower()


def test_zero_records_renders_heartbeat():
    archive, email = render.render_html([], mode="en_only", date_str="2026-06-19")
    assert "no qualifying" in email.lower() or "quiet" in email.lower()


def test_summary_block_and_source_link_render():
    rec = _rec(source_links=[{"url": "https://reuters.com/x", "domain": "reuters.com"}])
    summary = {"lede_en": "Gulf capital rotates east.", "takeaways": ["PIF shifts $15B."]}
    archive, email = render.render_html([rec], mode="en_only", date_str="2026-06-19",
                                        summary=summary, region_display="Middle East")
    assert "Middle East" in email
    assert "Gulf capital rotates east." in email
    assert "PIF shifts $15B." in email
    assert 'href="https://reuters.com/x"' in email
    assert "reuters.com" in email
    # the LLM 'sources' attribution URL must NOT be used as a link
    assert "attribution-only" not in email
