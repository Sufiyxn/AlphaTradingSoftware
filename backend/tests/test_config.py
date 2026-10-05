import os
import unittest
from unittest.mock import patch

from backend.core.config import load_settings


class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_settings().port, 8000)

    def test_environment_overrides(self) -> None:
        with patch.dict(os.environ, {"APP_HOST": "0.0.0.0", "APP_PORT": "8123", "APP_ENV": "test"}, clear=True):
            settings = load_settings()
        self.assertEqual((settings.host, settings.port, settings.environment), ("0.0.0.0", 8123, "test"))


if __name__ == "__main__":
    unittest.main()
