import pytest

from vertex.buy_rule import evaluate, max_all_in_cost, minimum_next_bid, sell_side_net


def test_sell_side_net_uses_exact_fees_and_fixed_postage():
    # $40 sale: 13.25% = 5.30, + 0.40 order fee, + 4.00 postage
    assert sell_side_net(40) == pytest.approx(40 - 5.30 - 0.40 - 4.00)


@pytest.mark.parametrize(
    "median, expected_all_in",
    [
        (20.00, 10.95),   # 54.75% of median: postage is a big share of a cheap card
        (40.00, 26.30),   # 65.75%
        (65.00, 45.48),   # just under the point where the 70% cap starts to bind
        (80.00, 56.00),   # formula says 57.00 (71.25%), capped at 70%
    ],
)
def test_max_all_in_flexes_with_value_and_caps_at_70_percent(median, expected_all_in):
    assert evaluate(median, 0.0).max_all_in == pytest.approx(expected_all_in)
    assert max_all_in_cost(median) <= 0.70 * median + 1e-9


def test_inbound_shipping_comes_out_of_the_max_bid():
    decision = evaluate(40.00, 4.99)
    assert decision.max_bid == pytest.approx(26.30 - 4.99)


def test_predicted_profit_is_target_margin_when_cap_not_binding():
    decision = evaluate(40.00, 4.99)
    assert decision.predicted_profit == pytest.approx(4.00, abs=0.01)  # 10% of 40


def test_predicted_profit_exceeds_margin_when_cap_binds():
    decision = evaluate(80.00, 0.0)
    # net 80 - 10.60 - 0.40 - 4.00 = 65.00; minus 56.00 paid
    assert decision.predicted_profit == pytest.approx(9.00)


@pytest.mark.parametrize("median", [19.99, 80.01, 5.00, 150.00])
def test_value_band_is_a_hard_filter(median):
    decision = evaluate(median, 0.0)
    assert not decision.is_bid
    assert "band" in decision.no_bid_reason


def test_unknown_shipping_means_no_bid():
    decision = evaluate(40.00, None)
    assert not decision.is_bid
    assert "shipping" in decision.no_bid_reason


def test_shipping_larger_than_ceiling_means_no_bid():
    decision = evaluate(20.00, 11.00)
    assert not decision.is_bid


def test_max_bid_never_rounds_up():
    decision = evaluate(33.33, 1.11)
    exact = 33.33 * (1 - 0.1325 - 0.10) - 4.40 - 1.11
    assert decision.max_bid <= exact
    assert exact - decision.max_bid < 0.02


def test_minimum_next_bid():
    assert minimum_next_bid(9.99, 0) == 9.99
    assert minimum_next_bid(9.99, 3) == 10.49
    assert minimum_next_bid(25.00, 1) == 26.00
    assert minimum_next_bid(0.99, 1) == 1.04
