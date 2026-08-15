#!/usr/bin/env python3
"""Run all tests and report failures.

Discovers every ``test_*.py`` under ``tests/`` (test_core, test_settings,
test_failover, test_api).
"""
import sys
import unittest

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover("tests", pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
