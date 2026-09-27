"""The eval's safety-critical metrics must stay green (protects the eval itself)."""

from eval import run_eval


def test_eval_safety_metrics_all_pass():
    m = run_eval.score()
    assert m["direction_ok"]
    assert m["recall"] == 1.0
    assert m["noise_excluded_ok"]          # injected noise stays out of the primary list
    assert m["investment_safety"] == 1.0
    assert m["price_creep_ok"]
    assert m["duplicate_ok"]
    assert m["aggregator_ok"]
    assert m["primary_size"] <= 12          # distilled to a shortlist, not a wall
    assert run_eval._safety_failures(m) == []
