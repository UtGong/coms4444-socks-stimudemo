"""Rule parity with the authoritative project engine, not implementation copies."""

import unittest

from core.engine import Engine
from players.greedy_player import GreedyPlayer

from analysis.compare import compare_pooling, compare_units
from analysis.sock_counterfactual import Simulator, actions, analyze


class CounterfactualParityTest(unittest.TestCase):
    def test_action_counts(self):
        self.assertEqual(len(actions(4)), 24)
        self.assertEqual(len(actions(5)), 80)

    def test_every_day_mode_includes_each_day(self):
        report = analyze(16, 1, 4, 5, 1, 100.0, samples=1, repeats=1,
                         horizon=3, every_day=True)
        self.assertEqual([row['day'] for row in report['sampled_turns']], [1, 2, 3, 4, 5])
        self.assertTrue(all(row['choices_evaluated'] == 24
                            for row in report['sampled_turns']))
        self.assertEqual(report['sampled_turns'][-1]['effective_horizon'], 1)

    def test_depleted_drawer_records_sockless_day_without_selecting_pair(self):
        report = analyze(40, 4, 4, 300, 1, 100.0, samples=1, repeats=1,
                         horizon=1, every_day=True)
        sockless = [row for row in report['sampled_turns']
                    if row['choices_evaluated'] == 0]
        self.assertTrue(sockless)
        self.assertIsNone(sockless[0]['baseline_action'])
        self.assertLess(len(sockless[0]['offered']), 2)

    def test_greedy_trajectories_match_engine(self):
        for unit, capacity, budget, seed, days in (
            (4, 40, None, 1, 60),
            (4, 40, 100.0, 7, 60),
            (5, 44, 50.0, 11, 60),
            (4, 40, 0.0, 3, 500),  # reaches worn-out holes and sockless days
        ):
            with self.subTest(unit=unit, budget=budget, seed=seed):
                engine = Engine([GreedyPlayer] * 4, capacity, unit, days, seed,
                                timeout=0, budget=budget)
                small = Simulator(capacity, 4, unit, days, seed, budget)
                for day in range(days):
                    record = engine.step()
                    small.step()
                    self.assertEqual(day + 1, small.day)
                    self.assertEqual(engine.total_spent, small.spent)
                    self.assertEqual(engine.pending_discards, small.pending)
                    self.assertEqual(engine.sockless, small.sockless)
                    self.assertEqual([engine.total_embarrassment(i) for i in range(4)],
                                     small.scores)
                    self.assertEqual([(s.color, s.shade) for s in engine.drawer],
                                     [(s.color, s.shade) for s in small.drawer])
                    self.assertEqual(record.day, small.day)
                    self.assertEqual(record.offered, small.last_offered)
                    self.assertEqual(record.embarrassment, small.last_daily_scores)
                    self.assertEqual(record.wear_idx,
                                     {i: action.wear for i, action in small.last_actions.items()})
                    self.assertEqual(record.discard_idx,
                                     {i: action.discard for i, action in small.last_actions.items()})
                self.assertEqual(engine.results()['total_spent'], small.spent)
                if budget == 0:
                    self.assertGreater(sum(small.holes.values()), 0)
                    self.assertGreater(sum(small.sockless), 0)

    def test_comparisons_keep_per_person_budget_and_capacity_valid(self):
        units = compare_units(40, 4, 20, 100.0, [1, 2])
        self.assertEqual(len(units['runs']), 2)
        for row in units['runs']:
            for side in ('a', 'b'):
                self.assertLessEqual(row[side]['annual_spend'], 100 / 4 * 360 / 20)
        pooled = compare_pooling(64, 4, 4, 20, 100.0, [1, 2])
        self.assertEqual(pooled['parameters']['solo_capacity'], 16)
        self.assertEqual(pooled['parameters']['solo_budget'], 25.0)
        with self.assertRaisesRegex(ValueError, 'multiple of 4'):
            compare_pooling(60, 4, 4, 20, 100.0, [1])


if __name__ == '__main__':
    unittest.main()
