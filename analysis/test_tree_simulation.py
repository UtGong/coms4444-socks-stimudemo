"""Tests for the bounded multi-roommate decision tree."""

import tempfile
import unittest
from pathlib import Path

from analysis.tree_simulation import (
    Scenario,
    State,
    budget_from_level,
    capacity_bounds,
    capacity_from_level,
    choices,
    connect,
    initial_state,
    parameter_pairs,
    run_trajectory,
    run_scenario,
    sequential_trajectories,
)


def scenario(**overrides) -> Scenario:
    values = {
        "roommates": 2,
        "capacity": 20,
        "sock_level": 0.0,
        "requested_socks_per_person": 10.0,
        "budget": 100,
        "budget_level": 1.0,
        "seed": 1,
        "days": 1,
        "unit": 4,
        "chance_samples": 1,
        "max_trajectories": 64,
        "max_states_per_day": 20,
    }
    values.update(overrides)
    return Scenario(**values)


class TreeSimulationTest(unittest.TestCase):
    def test_parameter_bounds_and_levels(self):
        self.assertEqual(capacity_bounds(1), (16, 20))
        self.assertEqual(capacity_bounds(10), (52, 200))
        self.assertEqual(capacity_from_level(10, 0), (52, 5.2))
        self.assertEqual(capacity_from_level(10, 1), (200, 20.0))
        self.assertEqual(budget_from_level(10, 1000, 0), 0)
        self.assertEqual(budget_from_level(10, 1000, 1), 40_000)

    def test_space_filling_design_has_thirteen_points(self):
        levels = (0.0, 0.25, 0.5, 0.75, 1.0)
        pairs = parameter_pairs(levels, levels, full_grid=False)
        self.assertEqual(len(pairs), 13)
        self.assertTrue({(0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0)} <= set(pairs))
        self.assertEqual(len(parameter_pairs(levels, levels, full_grid=True)), 25)

    def test_four_sock_hand_has_twenty_four_actions(self):
        self.assertEqual(len(choices(4)), 24)
        self.assertEqual(len(choices(3)), 6)
        self.assertEqual(len(choices(2)), 1)
        self.assertEqual(len(choices(1)), 1)  # automatic sockless marker

    def test_unworn_socks_return_before_next_roommate_draws(self):
        item = scenario(capacity=4)
        state = State(
            day=0,
            drawer=(("white", 249), ("white", 251), ("white", 253), ("white", 255)),
            pending_white=0,
            pending_black=0,
            spent=0,
            scores=(0, 0),
            sockless=(0, 0),
        )
        result = run_trajectory(state, item, 0, (0, 1), (0, 0))
        first_leftovers = {result.hands[0][2], result.hands[0][3]}
        self.assertEqual(set(result.hands[1]), first_leftovers)
        self.assertEqual(len(result.hands[1]), 2)
        self.assertEqual(len(result.state.drawer), 4)
        self.assertEqual(result.metrics.household_discard_count, 0)

    def test_sixth_same_color_discard_buys_pack(self):
        item = scenario(capacity=8, budget=10)
        state = State(
            day=0,
            drawer=tuple(("white", 255) for _ in range(8)),
            pending_white=5,
            pending_black=0,
            spent=0,
            scores=(0, 0),
            sockless=(0, 0),
        )
        main = next(
            index for index, action in enumerate(choices(4))
            if action.wear == (0, 1) and action.discard == (2,)
        )
        result = run_trajectory(state, item, 0, (0, 1), (main, None))
        self.assertEqual(result.state.spent, 10)
        self.assertEqual(result.state.pending_white, 0)
        self.assertEqual(len(result.state.drawer), 13)
        self.assertEqual(result.metrics.packs_bought, 1)

    def test_sequential_coverage_records_action_dependent_hands(self):
        item = scenario(max_trajectories=64)
        state = initial_state(item)
        trajectories, theoretical, _, explored = sequential_trajectories(state, item, 0)
        self.assertEqual(theoretical, 24**2)
        self.assertLessEqual(len(trajectories), 64)
        self.assertGreater(explored, 0)
        self.assertTrue(all(len(result.hands) == 2 for result in trajectories))

    def test_ten_roommates_cover_all_baseline_context_actions(self):
        item = scenario(
            roommates=10,
            capacity=80,
            requested_socks_per_person=8.0,
            max_trajectories=256,
        )
        trajectories, _, _, _ = sequential_trajectories(initial_state(item), item, 0)
        self.assertLessEqual(len(trajectories), 256)
        for player in range(10):
            self.assertEqual(
                {trajectory.action_indices[player] for trajectory in trajectories},
                set(range(24)),
            )

    def test_database_run_is_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "tree.sqlite")
            item = scenario()
            self.assertTrue(run_scenario(connection, item, "test", progress=False))
            first = connection.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
            self.assertGreater(first, 0)
            self.assertFalse(run_scenario(connection, item, "test", progress=False))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM transitions").fetchone()[0], first
            )
            connection.close()


if __name__ == "__main__":
    unittest.main()
