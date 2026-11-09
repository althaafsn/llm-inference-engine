"""Tests for the adaptive admission policy."""

import unittest

from llm_engine.adaptive import next_limit


class AdaptiveTests(unittest.TestCase):
    def test_waits_for_enough_samples(self):
        self.assertEqual(next_limit(4, 5000, 5, 1, 8, 400), 4)

    def test_decreases_above_deadband(self):
        self.assertEqual(next_limit(5, 1000, 30, 1, 8, 400), 4)

    def test_increases_below_deadband(self):
        self.assertEqual(next_limit(4, 200, 30, 1, 8, 400), 5)

    def test_holds_inside_deadband(self):
        self.assertEqual(next_limit(4, 420, 30, 1, 8, 400), 4)

    def test_clamps_to_bounds(self):
        self.assertEqual(next_limit(8, 200, 30, 1, 8, 400), 8)
        self.assertEqual(next_limit(1, 2000, 30, 1, 8, 400), 1)

    def test_zero_min_disables_policy(self):
        self.assertEqual(next_limit(4, 2000, 30, 0, 8, 400), 4)

    def test_invalid_bounds_return_current(self):
        self.assertEqual(next_limit(4, 2000, 30, 6, 4, 400), 4)


if __name__ == "__main__":
    unittest.main()
