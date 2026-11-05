"""Reference values cross-checked against SciPy (fisher_exact, false_discovery_control)."""
import pytest

from pipeline.metrics import stats


def test_wilson_reference():
    lo, hi = stats.wilson(5, 10)
    assert lo == pytest.approx(0.236593, abs=1e-6) and hi == pytest.approx(0.763407, abs=1e-6)
    assert stats.wilson(0, 20)[0] == 0.0
    assert stats.wilson(0, 0) is None


@pytest.mark.parametrize("k1,n1,k2,n2,expected", [
    (1, 10, 11, 14, 0.0027594),   # classic 2x2 example
    (8, 10, 1, 6, 0.0349650),
    (50, 100, 50, 100, 1.0),
])
def test_fisher_exact_reference(k1, n1, k2, n2, expected):
    assert stats.fisher_exact(k1, n1, k2, n2) == pytest.approx(expected, abs=1e-6)


def test_bh_matches_scipy():
    q = stats.bh_adjust([0.01, 0.04, 0.03, 0.2, None])
    assert q[:4] == pytest.approx([0.04, 0.0533333, 0.0533333, 0.2], abs=1e-6) and q[4] is None


def test_correlation_ci():
    ci, p = stats.correlation_ci_p(0.5, 24)
    assert ci == pytest.approx((0.121011, 0.751767), abs=1e-5)
    assert p == pytest.approx(0.011828, abs=1e-5)
    assert stats.correlation_ci_p(None, 24) == (None, None)


def test_planted_patterns_are_significant_and_noise_is_not(tmp_path):
    """On fixtures: the planted webinar campaign must test as significantly better;
    industry is assigned at random, so no industry should test significant."""
    from pipeline.extract.fixtures import generate
    from pipeline.metrics.compute import compute
    from pipeline.transform.normalize import build_db

    m = {x.id: x for x in compute(build_db(generate(), tmp_path / "w.sqlite"))}
    assert m["test.campaign.cmp-001.contact_to_deal_rate"].significant is True
    assert m["test.campaign.cmp-001.contact_to_deal_rate"].value > 0
    assert m["test.campaign.cmp-005.contact_to_deal_rate"].significant is True
    assert m["test.campaign.cmp-005.contact_to_deal_rate"].value < 0
    assert m["corr.sessions_vs_new_contacts.lag1"].significant is True
    assert not any(x.significant for k, x in m.items() if k.startswith("test.industry."))
    assert m["campaign.cmp-001.win_rate"].ci_low < m["campaign.cmp-001.win_rate"].value < m["campaign.cmp-001.win_rate"].ci_high
