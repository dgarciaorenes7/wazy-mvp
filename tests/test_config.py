"""Settings: good values load, wrong values stop the server with a message naming the setting (fail closed),
and the .env file is read the way people expect (comments, quotes, the real environment wins).

Run: python3 -m unittest tests.test_config -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wazy.config import DEFAULT_DB, DEFAULT_MODEL, Config, ConfigError, load_config, parse_dotenv

SID = "AC" + "0123456789abcdef" * 2
TOKEN = "fedcba9876543210" * 2
FROM = "whatsapp:+14155238886"
CONTENT = "HX" + "abcdef0123456789" * 2
URL = "https://wazy.example.com"
TWILIO = {"TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": TOKEN, "TWILIO_WHATSAPP_FROM": FROM,
          "WAZY_PUBLIC_URL": URL}


def config(**env: str) -> Config:
    return load_config(env=env)


class Defaults(unittest.TestCase):
    def test_nothing_set_gives_the_local_defaults(self):
        c = config()
        self.assertEqual((c.host, c.port, c.tz), ("127.0.0.1", 8765, "Europe/Madrid"))
        self.assertEqual((c.db_path, c.ai_model, c.later_minutes), (DEFAULT_DB, DEFAULT_MODEL, None))
        self.assertEqual((c.anthropic_api_key, c.access_code, c.admin_code, c.public_url), ("", "", "", ""))
        self.assertFalse(c.twilio_enabled)

    def test_default_database_is_inside_the_project(self):
        self.assertTrue(DEFAULT_DB.endswith(os.path.join("data", "wazy.db")))

    def test_dataclass_defaults_match_load_config(self):
        self.assertEqual(Config(), config())

    def test_empty_values_count_as_not_set(self):
        c = config(WAZY_PORT="", WAZY_TZ=" ", ANTHROPIC_API_KEY="", WAZY_LATER_MINUTES="", WAZY_DB="")
        self.assertEqual((c.port, c.tz, c.anthropic_api_key, c.later_minutes, c.db_path),
                         (8765, "Europe/Madrid", "", None, DEFAULT_DB))

    def test_values_are_trimmed(self):
        c = config(WAZY_PORT=" 9000 ", WAZY_LATER_MINUTES=" 2 ", WAZY_TZ=" UTC ")
        self.assertEqual((c.port, c.later_minutes, c.tz), (9000, 2, "UTC"))


class Ports(unittest.TestCase):
    def test_hosting_platform_port_is_used(self):
        self.assertEqual(config(PORT="10000").port, 10000)

    def test_wazy_port_wins_over_port(self):
        self.assertEqual(config(PORT="10000", WAZY_PORT="9000").port, 9000)

    def test_port_limits(self):
        self.assertEqual(config(WAZY_PORT="1").port, 1)
        self.assertEqual(config(WAZY_PORT="65535").port, 65535)

    def test_bad_port_names_the_setting_that_was_used(self):
        with self.assertRaisesRegex(ConfigError, "^PORT"):
            config(PORT="99999")
        with self.assertRaisesRegex(ConfigError, "^WAZY_PORT"):
            config(WAZY_PORT="abc", PORT="80")


# Good values: (settings, field, expected value)
GOOD = [
    ({"WAZY_HOST": "0.0.0.0"}, "host", "0.0.0.0"),
    ({"WAZY_HOST": "localhost"}, "host", "localhost"),
    ({"WAZY_TZ": "UTC"}, "tz", "UTC"),
    ({"WAZY_TZ": "America/New_York"}, "tz", "America/New_York"),
    ({"WAZY_TZ": "Europe/London"}, "tz", "Europe/London"),
    ({"WAZY_DB": "/tmp/wazy-test.db"}, "db_path", "/tmp/wazy-test.db"),
    ({"WAZY_DB": ":memory:"}, "db_path", ":memory:"),
    ({"ANTHROPIC_API_KEY": "sk-ant-api03-abc_DEF-123"}, "anthropic_api_key", "sk-ant-api03-abc_DEF-123"),
    ({"WAZY_AI_MODEL": "claude-haiku-4-5"}, "ai_model", "claude-haiku-4-5"),
    ({"WAZY_LATER_MINUTES": "1"}, "later_minutes", 1),
    ({"WAZY_LATER_MINUTES": "1440"}, "later_minutes", 1440),
    ({"WAZY_LATER_MINUTES": "30"}, "later_minutes", 30),
    ({"WAZY_ACCESS_CODE": "abcd1234"}, "access_code", "abcd1234"),
    ({"WAZY_ACCESS_CODE": "Class-of_2026"}, "access_code", "Class-of_2026"),
    ({"WAZY_ADMIN_CODE": "presenter-123"}, "admin_code", "presenter-123"),
    ({"WAZY_PUBLIC_URL": "https://wazy.example.com/"}, "public_url", "https://wazy.example.com"),
    ({"WAZY_PUBLIC_URL": "https://abc.example.com:8443"}, "public_url", "https://abc.example.com:8443"),
]

# Bad values: (settings, the setting the message must name)
BAD = [
    ({"WAZY_PORT": "abc"}, "WAZY_PORT"),
    ({"WAZY_PORT": "0"}, "WAZY_PORT"),
    ({"WAZY_PORT": "65536"}, "WAZY_PORT"),
    ({"WAZY_PORT": "-1"}, "WAZY_PORT"),
    ({"WAZY_PORT": "80.5"}, "WAZY_PORT"),
    ({"WAZY_PORT": "1_000"}, "WAZY_PORT"),
    ({"WAZY_HOST": "bad host"}, "WAZY_HOST"),
    ({"WAZY_TZ": "Mars/Olympus"}, "WAZY_TZ"),
    ({"WAZY_TZ": "../etc/passwd"}, "WAZY_TZ"),
    ({"WAZY_TZ": "Madrid"}, "WAZY_TZ"),
    ({"ANTHROPIC_API_KEY": "not-a-key"}, "ANTHROPIC_API_KEY"),
    ({"ANTHROPIC_API_KEY": "sk-123"}, "ANTHROPIC_API_KEY"),
    ({"ANTHROPIC_API_KEY": "sk-ant-"}, "ANTHROPIC_API_KEY"),
    ({"ANTHROPIC_API_KEY": "sk-ant-abc def"}, "ANTHROPIC_API_KEY"),
    ({"WAZY_AI_MODEL": "claude haiku"}, "WAZY_AI_MODEL"),
    ({"WAZY_LATER_MINUTES": "0"}, "WAZY_LATER_MINUTES"),
    ({"WAZY_LATER_MINUTES": "1441"}, "WAZY_LATER_MINUTES"),
    ({"WAZY_LATER_MINUTES": "abc"}, "WAZY_LATER_MINUTES"),
    ({"WAZY_LATER_MINUTES": "1.5"}, "WAZY_LATER_MINUTES"),
    ({"WAZY_LATER_MINUTES": "-5"}, "WAZY_LATER_MINUTES"),
    ({"WAZY_LATER_MINUTES": "five"}, "WAZY_LATER_MINUTES"),
    ({"WAZY_ACCESS_CODE": "short"}, "WAZY_ACCESS_CODE"),
    ({"WAZY_ACCESS_CODE": "1234567"}, "WAZY_ACCESS_CODE"),
    ({"WAZY_ACCESS_CODE": "has spaces in it"}, "WAZY_ACCESS_CODE"),
    ({"WAZY_ACCESS_CODE": "semi;colon12"}, "WAZY_ACCESS_CODE"),
    ({"WAZY_ADMIN_CODE": "elevenchars"}, "WAZY_ADMIN_CODE"),
    ({"WAZY_ADMIN_CODE": "same-as-access", "WAZY_ACCESS_CODE": "same-as-access"}, "WAZY_ADMIN_CODE"),
    ({"TWILIO_ACCOUNT_SID": SID}, "TWILIO_AUTH_TOKEN"),
    ({"TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": TOKEN}, "TWILIO_WHATSAPP_FROM"),
    ({"TWILIO_WHATSAPP_FROM": FROM}, "TWILIO_ACCOUNT_SID"),
    ({"TWILIO_CONTENT_SID": CONTENT}, "TWILIO_ACCOUNT_SID"),
    ({**TWILIO, "TWILIO_ACCOUNT_SID": "AC123"}, "TWILIO_ACCOUNT_SID"),
    ({**TWILIO, "TWILIO_ACCOUNT_SID": "XX" + "0" * 32}, "TWILIO_ACCOUNT_SID"),
    ({**TWILIO, "TWILIO_AUTH_TOKEN": "not-hex-not-hex-not-hex-not-hex!"}, "TWILIO_AUTH_TOKEN"),
    ({**TWILIO, "TWILIO_AUTH_TOKEN": "abc123"}, "TWILIO_AUTH_TOKEN"),
    ({**TWILIO, "TWILIO_WHATSAPP_FROM": "+14155238886"}, "TWILIO_WHATSAPP_FROM"),
    ({**TWILIO, "TWILIO_WHATSAPP_FROM": "whatsapp:14155238886"}, "TWILIO_WHATSAPP_FROM"),
    ({**TWILIO, "TWILIO_WHATSAPP_FROM": "whatsapp:+1 415 523"}, "TWILIO_WHATSAPP_FROM"),
    ({**TWILIO, "TWILIO_CONTENT_SID": "HX12"}, "TWILIO_CONTENT_SID"),
    ({**TWILIO, "WAZY_PUBLIC_URL": ""}, "WAZY_PUBLIC_URL"),
    ({**TWILIO, "WAZY_PUBLIC_URL": "http://wazy.example.com"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "wazy.example.com"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "https://"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "https://user:pw@wazy.example.com"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "https://wazy.example.com/?a=1"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "https://wazy.example.com/#top"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "https://wazy example.com"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "https://wazy.example.com:99999"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "https://wazy.example.com:abc"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "https://wazy.example.com:0"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "https://wazy.example.com/?"}, "WAZY_PUBLIC_URL"),
    ({"WAZY_PUBLIC_URL": "https://wazy.example.com#"}, "WAZY_PUBLIC_URL"),
]


class Table(unittest.TestCase):
    def test_good_values_load(self):
        for env, name, expected in GOOD:
            with self.subTest(env=env):
                self.assertEqual(getattr(config(**env), name), expected)

    def test_bad_values_refuse_to_start_and_name_the_setting(self):
        for env, name in BAD:
            with self.subTest(env=env):
                with self.assertRaises(ConfigError) as caught:
                    config(**env)
                self.assertIn(name, str(caught.exception))

    def test_tables_are_big_enough(self):
        self.assertGreaterEqual(len(GOOD) + len(BAD), 60)


class Twilio(unittest.TestCase):
    def test_all_set_switches_whatsapp_on(self):
        c = config(**TWILIO)
        self.assertTrue(c.twilio_enabled)
        self.assertEqual((c.twilio_account_sid, c.twilio_auth_token, c.twilio_from, c.public_url),
                         (SID, TOKEN, FROM, URL))
        self.assertEqual(c.twilio_content_sid, "")

    def test_content_template_is_optional_and_kept(self):
        self.assertEqual(config(**TWILIO, TWILIO_CONTENT_SID=CONTENT).twilio_content_sid, CONTENT)

    def test_uppercase_hex_is_accepted(self):
        self.assertTrue(config(**{**TWILIO, "TWILIO_AUTH_TOKEN": TOKEN.upper()}).twilio_enabled)

    def test_public_url_alone_is_fine_without_twilio(self):
        c = config(WAZY_PUBLIC_URL=URL)
        self.assertFalse(c.twilio_enabled)
        self.assertEqual(c.public_url, URL)

    def test_partial_settings_list_every_missing_one(self):
        with self.assertRaises(ConfigError) as caught:
            config(TWILIO_ACCOUNT_SID=SID)
        self.assertIn("TWILIO_AUTH_TOKEN", str(caught.exception))
        self.assertIn("TWILIO_WHATSAPP_FROM", str(caught.exception))


class Secrets(unittest.TestCase):
    def test_messages_never_repeat_a_secret(self):
        cases = [{"ANTHROPIC_API_KEY": "sk-ant-SECRET VALUE"}, {"WAZY_ACCESS_CODE": "SECRET!"},
                 {"WAZY_ADMIN_CODE": "SECRET"}, {**TWILIO, "TWILIO_AUTH_TOKEN": "SECRET"},
                 {**TWILIO, "TWILIO_ACCOUNT_SID": "SECRET"},
                 {"WAZY_PUBLIC_URL": "https://user:SECRET@wazy.example.com"}]
        for env in cases:
            with self.subTest(env=sorted(env)):
                with self.assertRaises(ConfigError) as caught:
                    config(**env)
                self.assertNotIn("SECRET", str(caught.exception))

    def test_repr_hides_secrets(self):
        c = config(ANTHROPIC_API_KEY="sk-ant-hidden", WAZY_ACCESS_CODE="hidden-code",
                   WAZY_ADMIN_CODE="hidden-admin-code", **TWILIO)
        self.assertNotIn("hidden", repr(c))
        self.assertNotIn(TOKEN, repr(c))

    def test_config_cannot_be_changed_after_loading(self):
        with self.assertRaises(Exception):
            config().port = 1  # type: ignore[misc]


class DotenvParsing(unittest.TestCase):
    def test_comments_blank_lines_and_spaces(self):
        text = "# a comment\n\n   # indented comment\nWAZY_PORT = 9000\nWAZY_TZ=UTC\n"
        self.assertEqual(parse_dotenv(text), {"WAZY_PORT": "9000", "WAZY_TZ": "UTC"})

    def test_quotes_are_removed_and_keep_what_is_inside(self):
        text = "A=\"hello world\"\nB='single # not a comment'\nC=\"\"\n"
        self.assertEqual(parse_dotenv(text), {"A": "hello world", "B": "single # not a comment", "C": ""})

    def test_inline_comment_after_an_unquoted_value(self):
        self.assertEqual(parse_dotenv("WAZY_PORT=9000   # my port\nX=a#b\n"), {"WAZY_PORT": "9000", "X": "a#b"})

    def test_comment_after_a_quoted_value(self):
        self.assertEqual(parse_dotenv('X="a b" # note\n'), {"X": "a b"})

    def test_export_prefix_is_allowed(self):
        self.assertEqual(parse_dotenv("export WAZY_TZ=UTC\n"), {"WAZY_TZ": "UTC"})

    def test_empty_value(self):
        self.assertEqual(parse_dotenv("ANTHROPIC_API_KEY=\n"), {"ANTHROPIC_API_KEY": ""})

    def test_equals_sign_inside_the_value(self):
        self.assertEqual(parse_dotenv("X=a=b=c\n"), {"X": "a=b=c"})

    def test_windows_line_endings(self):
        self.assertEqual(parse_dotenv("A=1\r\nB=2\r\n"), {"A": "1", "B": "2"})

    def test_line_without_equals_is_refused_with_its_number(self):
        with self.assertRaisesRegex(ConfigError, "line 2"):
            parse_dotenv("A=1\njust some words\n")

    def test_bad_key_is_refused(self):
        with self.assertRaises(ConfigError):
            parse_dotenv("1BAD=x\n")

    def test_unclosed_quote_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "line 1"):
            parse_dotenv('A="never closed\n')


class DotenvFile(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / ".env"

    def write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_values_from_the_file_are_used(self):
        path = self.write("WAZY_PORT=9100\nWAZY_LATER_MINUTES=2\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            c = load_config(dotenv=path)
        self.assertEqual((c.port, c.later_minutes), (9100, 2))

    def test_real_environment_wins_over_the_file(self):
        path = self.write("WAZY_PORT=9100\nWAZY_TZ=UTC\n")
        with mock.patch.dict(os.environ, {"WAZY_PORT": "9200"}, clear=True):
            c = load_config(dotenv=path)
        self.assertEqual((c.port, c.tz), (9200, "UTC"))

    def test_given_env_wins_over_a_given_file(self):
        path = self.write("WAZY_PORT=9100\nWAZY_TZ=UTC\n")
        c = load_config(env={"WAZY_PORT": "9300"}, dotenv=path)
        self.assertEqual((c.port, c.tz), (9300, "UTC"))

    def test_given_env_alone_ignores_the_real_environment(self):
        with mock.patch.dict(os.environ, {"WAZY_PORT": "9400"}):
            self.assertEqual(load_config(env={}).port, 8765)

    def test_missing_file_is_fine(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config(dotenv=Path(self.folder.name) / "nope.env").port, 8765)

    def test_a_bad_value_in_the_file_also_stops_the_server(self):
        path = self.write("WAZY_TZ=Nowhere/Land\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ConfigError, "WAZY_TZ"):
                load_config(dotenv=path)

    def test_file_with_a_quoted_secret(self):
        path = self.write('ANTHROPIC_API_KEY="sk-ant-abc123"\n')
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config(dotenv=path).anthropic_api_key, "sk-ant-abc123")

    def test_example_file_loads_as_it_is(self):
        example = Path(__file__).resolve().parent.parent / ".env.example"
        self.assertEqual(load_config(env={}, dotenv=example), Config())

    def test_example_file_mentions_every_setting(self):
        example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
        for name in ("WAZY_HOST", "WAZY_PORT", "PORT", "WAZY_TZ", "WAZY_DB", "ANTHROPIC_API_KEY", "WAZY_AI_MODEL",
                     "WAZY_LATER_MINUTES", "WAZY_ACCESS_CODE", "WAZY_ADMIN_CODE", "WAZY_PUBLIC_URL",
                     "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_CONTENT_SID"):
            self.assertIn(name, example)


if __name__ == "__main__":
    unittest.main()
