"""Checks for conditional-strategy state classification."""

import unittest

from analysis.strategy_rules import (
    Action,
    direct_pack_trigger,
    distribution_features,
    remaining_budget_bucket,
    stock_bucket,
)


class StrategyRulesTest(unittest.TestCase):
    def test_stock_thresholds_encode_next_day_feasibility(self):
        self.assertEqual(stock_bucket(7, 4), "<2 socks/person")
        self.assertEqual(stock_bucket(8, 4), "2-<4 socks/person")
        self.assertEqual(stock_bucket(16, 4), "4-<6 socks/person")

    def test_remaining_budget_is_normalized_by_roommates(self):
        self.assertEqual(remaining_budget_bucket(9.99, 4), "0 replacement socks/person")
        self.assertEqual(remaining_budget_bucket(10.0, 4), "<2 replacement socks/person")
        self.assertEqual(remaining_budget_bucket(None, 4), "unlimited")

    def test_identical_socks_are_pairable(self):
        features = distribution_features([255, 255, 0, 0])
        self.assertEqual(features["partnerable"], 1.0)
        self.assertEqual(features["imbalance"], 0.0)

    def test_pending_counts_do_not_combine_across_colors(self):
        action = Action(
            wear="[0, 1]", discard=(2, 3), discard_shades=(255, 0), immediate=0,
            focal=0, household=0, spend=0, household_sockless=0, focal_sockless=0,
            train_focal=0, train_household=0, train_spend=0,
            train_household_sockless=0, train_focal_sockless=0,
        )
        self.assertFalse(direct_pack_trigger(action, 4, 4, 10.0))
        self.assertTrue(direct_pack_trigger(action, 5, 0, 10.0))
        self.assertFalse(direct_pack_trigger(action, 5, 0, 9.99))


if __name__ == "__main__":
    unittest.main()
