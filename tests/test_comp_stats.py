from vertex.comp_stats import passes_gate, summarize


def test_iqr_trims_high_and_low_outliers():
    prices = [30, 31, 32, 33, 34, 35, 36, 37, 38, 2, 250]
    summary = summarize(prices)
    assert 2 not in summary.kept
    assert 250 not in summary.kept
    assert summary.raw_count == 11
    assert summary.count == 9
    assert summary.median == 34


def test_gate_counts_comps_after_trimming():
    # 9 raw sales, but 2 are outliers: only 7 survive, so no bid.
    prices = [30, 31, 32, 33, 34, 35, 36, 1, 400]
    summary = summarize(prices)
    assert summary.raw_count == 9
    assert summary.count == 7
    assert not passes_gate(summary)


def test_gate_passes_with_eight_clean_comps():
    summary = summarize([30, 31, 32, 33, 34, 35, 36, 37])
    assert summary.count == 8
    assert passes_gate(summary)
    assert summary.median == 33.5


def test_tiny_samples_are_not_trimmed_and_fail_gate():
    summary = summarize([10, 20, 30])
    assert summary.count == 3
    assert not passes_gate(summary)


def test_empty():
    summary = summarize([])
    assert summary.median is None
    assert not passes_gate(summary)
