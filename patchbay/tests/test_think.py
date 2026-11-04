"""
How hard each model thinks (model_labels.think_value): Qwen 3.8 models get "low" instead of their slowest level,
orca-coder always "low", config.json "model_think" wins, and other models keep the old on-for-hard-jobs rule.
Run from the patchbay folder:

  python3 -m unittest discover -s tests -t .
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_labels  # noqa: E402

URL = "http://fake"


def known(name, renderer, caps=("completion", "tools", "thinking")):
    model_labels._CAPS[(URL, name)] = list(caps)
    model_labels._RENDERER[(URL, name)] = renderer


class Think(unittest.TestCase):
    def setUp(self):
        known("orca-coder:latest", "qwen3.8")
        known("qwen3.8:27b", "qwen3.8")
        known("qwen36-uncensored-fixed:latest", "qwen3.5")
        known("gemma4:26b", "")

    def test_orca_coder_is_low(self):
        self.assertEqual(model_labels.think_setting({}, URL, "orca-coder:latest"), "low")
        self.assertEqual(model_labels.think_value({}, URL, "orca-coder:latest", False), "low")
        self.assertEqual(model_labels.think_value({}, URL, "orca-coder:latest", True), "low")

    def test_daily_qwen36_is_off(self):
        self.assertIs(model_labels.think_value({}, URL, "qwen36-uncensored-fixed:latest", True), False)
        known("qwen36-levels:latest", "qwen3.8")      # same weights, newer format: still off (low tested worse)
        self.assertIs(model_labels.think_value({}, URL, "qwen36-levels:latest", True), False)

    def test_qwen38_format_defaults_to_low(self):
        self.assertEqual(model_labels.think_value({}, URL, "qwen3.8:27b", True), "low")

    def test_other_models_keep_old_rule(self):
        known("qwen3.6:35b-a3b", "qwen3.5")
        for name in ("qwen3.6:35b-a3b", "gemma4:26b"):
            self.assertEqual(model_labels.think_setting({}, URL, name), "auto")
            self.assertIs(model_labels.think_value({}, URL, name, True), True)
            self.assertIs(model_labels.think_value({}, URL, name, False), False)

    def test_config_wins(self):
        cfg = {"model_think": {"orca-coder": "off", "qwen36": "low"}}
        self.assertIs(model_labels.think_value(cfg, URL, "orca-coder:latest", True), False)
        # A model without levels only knows on and off: "low" means on.
        self.assertIs(model_labels.think_value(cfg, URL, "qwen36-uncensored-fixed:latest", False), True)
        cfg = {"model_think": {"qwen3.8": "high"}}
        self.assertEqual(model_labels.think_value(cfg, URL, "qwen3.8:27b", False), "xhigh")

    def test_bad_config_ignored(self):
        cfg = {"model_think": {"orca-coder": "max"}}
        self.assertEqual(model_labels.think_value(cfg, URL, "orca-coder:latest", True), "low")
        self.assertEqual(model_labels.think_value({"model_think": "low"}, URL, "orca-coder:latest", True), "low")


if __name__ == "__main__":
    unittest.main()
