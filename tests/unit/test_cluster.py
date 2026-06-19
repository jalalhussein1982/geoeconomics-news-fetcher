from geoecon.stages import cluster


def test_fingerprint_prefers_title_then_url():
    assert cluster.fingerprint("Saudi Trade Deal", None) == cluster.fingerprint("  saudi   trade deal ", None)
    assert cluster.fingerprint(None, "http://a/b") == cluster.fingerprint(None, "http://a/b")


def test_run_creates_one_cluster_per_article():
    from geoecon.state.models import Article
    arts = [Article(1, "u1", "h1", "d", "MENA", [], None, None, "ar", "T1", "body", "extracted"),
            Article(2, "u2", "h2", "d", "MENA", [], None, None, "es", None, None, "failed")]
    made = []
    class FakeRepo:
        def articles_for_run(self, rid): return arts
        def insert_cluster(self, rid, fp, region, langs, n, framing_divergent=False):
            made.append((fp, region, langs, n)); return len(made)
        def add_membership(self, cid, aid, frame_label=None): pass
    metrics = cluster.run(cfg=None, repo=FakeRepo(), run_id=1)
    assert metrics["clusters"] == 1  # failed article excluded
    assert made[0][1] == "MENA" and made[0][2] == ["ar"]
