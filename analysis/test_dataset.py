"""Persistence and resume checks for the recorded counterfactual dataset."""

import tempfile
import unittest
from pathlib import Path

from analysis.generate_dataset import connect, counts, run_scenario, scenarios


class DatasetTest(unittest.TestCase):
    def test_scenario_is_recorded_and_resume_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / 'data.sqlite')
            scenario = scenarios('smoke')[0]
            ran, decisions = run_scenario(connection, scenario, 'smoke')
            first = counts(connection)
            self.assertTrue(ran)
            self.assertGreater(decisions, 0)
            self.assertGreater(first['rollouts'], 0)

            ran_again, decisions_again = run_scenario(connection, scenario, 'smoke')
            self.assertFalse(ran_again)
            self.assertEqual(decisions_again, 0)
            self.assertEqual(counts(connection), first)
            connection.close()


if __name__ == '__main__':
    unittest.main()
