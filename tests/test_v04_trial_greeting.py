"""Focused coverage for the trial greeting helper."""

import unittest

from v04_trial_greeting import greet


class GreetingTests(unittest.TestCase):
    def test_greet_ordinary_name(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")


if __name__ == "__main__":
    unittest.main()
