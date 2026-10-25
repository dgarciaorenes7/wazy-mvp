"""Settings: bad values stop the server (fail closed), and hosting platforms work out of the box.
Run: python3 -m unittest discover tests"""
import os
import unittest
from unittest import mock

from wazy.config import Config, ConfigError

CLEAN = {k: v for k, v in os.environ.items() if not k.startswith("WAZY_") and k not in ("PORT", "ANTHROPIC_API_KEY")}


def config(**env):
    with mock.patch.dict(os.environ, {**CLEAN, **env}, clear=True), mock.patch("wazy.config.load_dotenv"):
        return Config()


class Settings(unittest.TestCase):
    def test_defaults(self):
        c = config()
        self.assertEqual((c.host, c.port), ("127.0.0.1", 8765))
        self.assertIn(c.timezone_name, ("Europe/Madrid", "this computer's local time"))

    def test_hosting_platform_port_is_used(self):
        self.assertEqual(config(PORT="10000").port, 10000)
        self.assertEqual(config(PORT="10000", WAZY_PORT="9000").port, 9000)

    def test_wrong_values_refuse_to_start(self):
        for env in ({"WAZY_PORT": "abc"}, {"PORT": "99999"}, {"WAZY_ACCESS_CODE": "short"},
                    {"ANTHROPIC_API_KEY": "not-a-key"}, {"WAZY_TZ": "Mars/Olympus"}):
            with self.assertRaises(ConfigError, msg=str(env)):
                config(**env)


if __name__ == "__main__":
    unittest.main()
