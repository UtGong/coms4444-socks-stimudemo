"""Tests for the bounded multi-roommate decision tree."""

import tempfile
import unittest
from pathlib import Path

from analysis.tree_simulation import (
    Scenario,
    State,
    apply_profile,
    choices,
    connect,
    draw_day,
    initial_state,
    joint_profiles,
    run_scenario,
)


def scenario(**overrides) -> Scenario:
    values = {
        "roommates": 2,
        "capacity": 20,
        "requested_socks_per_person": 10.0,
        "budget": 100,
        "seed": 1,
        "days": 1,
        "unit": 4,
        "chance_samples": 1,
        "max_joint_profiles": 64,
        "max_states_per_day": 20,
    }
    values.update(overrides)
    return Scenario(**values)


class TreeSimulationTest(unittest.TestCase):
    def test_four_sock_hand_has_twenty_four_actions(self):
        self.assertEqual(len(choices(4)), 24)
        self.assertEqual(len(choices(3)), 6)
        self.assertEqual(len(choices(2)), 1)
        self.assertEqual(len(choices(1)), 1)  # automatic sockless marker

    def test_profile_design_covers_every_individual_action(self):
        hands = [
            [("white", 255)] * 4,
            [("black", 0)] * 4,
        ]
        profiles, theoretical, pair_possible, pair_explored = joint_profiles(hands, 64, 7)
        self.assertEqual(theoretical, 24**2)
        self.assertEqual({profile[0] for profile in profiles}, set(range(24)))
        self.assertEqual({profile[1] for profile in profiles}, set(range(24)))
        self.assertEqual(pair_possible, 24**2)
        self.assertGreater(pair_explored, 0)

    def test_keep_actions_conserve_pristine_inventory(self):
        item = scenario()
        state = initial_state(item)
        order, hands, leftover = draw_day(state, item, 0)
        action_lists = [choices(len(hand)) for hand in hands]
        profile = tuple(
            next(index for index, action in enumerate(actions) if not action.discard)
            for actions in action_lists
        )
        child, metrics, _ = apply_profile(
            state, item, 0, order, hands, leftover, profile, action_lists
        )
        self.assertEqual(len(child.drawer), item.capacity)
        self.assertEqual(child.spent, 0)
        self.assertEqual(metrics.household_discard_count, 0)
        self.assertEqual(metrics.household_holes, 0)

    def test_sixth_same_color_discard_buys_pack(self):
        item = scenario(budget=10)
        base = initial_state(item)
        state = State(
            day=0,
            drawer=base.drawer,
            pending_white=5,
            pending_black=0,
            spent=0,
            scores=base.scores,
            sockless=base.sockless,
        )
        hands = [[("white", 255)] * 4, [("black", 0)] * 4]
        leftover = list(base.drawer[8:])
        action_lists = [choices(4), choices(4)]
        main = next(
            index for index, action in enumerate(action_lists[0])
            if action.wear == (0, 1) and action.discard == (2,)
        )
        other = next(index for index, action in enumerate(action_lists[1]) if not action.discard)
        child, metrics, _ = apply_profile(
            state, item, 0, [0, 1], hands, leftover, (main, other), action_lists
        )
        self.assertEqual(child.spent, 10)
        self.assertEqual(child.pending_white, 0)
        self.assertEqual(len(child.drawer), 25)
        self.assertEqual(metrics.packs_bought, 1)

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
