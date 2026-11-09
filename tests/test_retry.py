"""Tests for retry delay and header parsing."""

import unittest

from llm_engine.bench import parse_retry_after, retry_delay_seconds


class RetryTests(unittest.TestCase):
    def test_parse_numeric_retry_after(self):
        self.assertEqual(parse_retry_after("2"), 2.0)

    def test_parse_missing_retry_after(self):
        self.assertIsNone(parse_retry_after(None))

    def test_parse_invalid_retry_after(self):
        self.assertIsNone(parse_retry_after("not-a-date"))

    def test_exponential_backoff(self):
        self.assertEqual(retry_delay_seconds(1, None, 100, 1000, 0), 0.1)
        self.assertEqual(retry_delay_seconds(3, None, 100, 1000, 0), 0.4)

    def test_backoff_is_capped(self):
        self.assertEqual(retry_delay_seconds(10, None, 100, 1000, 0), 1.0)

    def test_retry_after_wins(self):
        self.assertEqual(retry_delay_seconds(1, 2.0, 100, 1000, 0), 2.0)


if __name__ == "__main__":
    unittest.main()
