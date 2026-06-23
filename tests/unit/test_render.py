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


def test_format_figures_spacing_and_dedup():
    from geoecon.stages import render
    figs = [{"value": "$318 billion", "unit": "USD"},
            {"value": "$318 billion", "unit": "USD"},   # duplicate
            {"value": "1.10%", "unit": "%"},            # unit already a suffix
            {"value": "52,621.84", "unit": "points"}]
    out = render.format_figures(figs)
    assert "$318 billion USD" in out
    assert out.count("$318 billion USD") == 1          # de-duped
    assert "1.10%" in out and "%%" not in out
    assert "52,621.84 points" in out
    assert " · " in out                                # records joined with ` · ` separator


def test_format_figures_caps_to_max():
    figs = [{"value": str(i), "unit": ""} for i in range(1, 13)]  # 12 figures
    out = render.format_figures(figs, max_figures=8)
    assert len(out.split(" · ")) == 8


def test_format_figures_numeric_dedupe_locale_and_currency():
    figs = [{"value": "49.66%", "unit": "%"}, {"value": "49,66%", "unit": "%"},
            {"value": "US$72", "unit": ""}, {"value": "72", "unit": "USD"}]
    out = render.format_figures(figs)
    assert len(out.split(" · ")) == 2  # one percent value, one currency value


def test_format_figures_salience_orders_referenced_first():
    figs = [{"value": "999", "unit": ""},   # not in prose -> ranked last
            {"value": "42", "unit": ""}]     # appears in prose -> ranked first
    out = render.format_figures(figs, context_text="growth hit 42 this year")
    assert out == "42 · 999"


def test_format_figures_zero_means_no_cap():
    figs = [{"value": str(i), "unit": ""} for i in range(1, 13)]  # 12 figures
    assert len(render.format_figures(figs, max_figures=0).split(" · ")) == 12
