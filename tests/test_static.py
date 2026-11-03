"""Static checks for the two web pages (wazy/static/chat.html and demo.html).

These tests read the HTML as text and with html.parser; they do not run a browser. They check the
promises the pages make: text is never parsed as HTML, the exact API paths and JSON field names from
SPEC.md are used, nothing is loaded from other websites, and the chat is accessible in both languages.
"""
from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

STATIC = Path(__file__).resolve().parent.parent / "wazy" / "static"
CHAT = STATIC / "chat.html"
DEMO = STATIC / "demo.html"

# Elements that never have a closing tag.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class PageParser(HTMLParser):
    """Collects every start tag with its attributes, the script text, and any tag that is not closed properly."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: List[Tuple[str, Dict[str, Optional[str]]]] = []
        self.stack: List[str] = []
        self.problems: List[str] = []
        self.scripts: List[str] = []
        self.styles: List[str] = []
        self.doctype = ""
        self._in: Optional[str] = None

    def handle_decl(self, decl: str) -> None:
        self.doctype = decl

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self._in = tag
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        self._in = None
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append("unexpected </%s> (open: %s)" % (tag, self.stack[-3:]))
            if tag in self.stack:                      # recover so one slip is reported once
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in == "script":
            self.scripts.append(data)
        elif self._in == "style":
            self.styles.append(data)


def parse(path: Path) -> PageParser:
    parser = PageParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser


def find(parser: PageParser, tag: str, **wanted: str) -> List[Dict[str, Optional[str]]]:
    """All tags of one kind whose attributes include the given values (use attr_name='*' to only require presence)."""
    found = []
    for name, attrs in parser.tags:
        if name != tag:
            continue
        if all(key.replace("_", "-") in attrs and (value == "*" or attrs[key.replace("_", "-")] == value)
               for key, value in wanted.items()):
            found.append(attrs)
    return found


class PageBasics(unittest.TestCase):
    """Both pages: present, well formed, safe, self-contained."""

    pages = (CHAT, DEMO)

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = {p.name: p.read_text(encoding="utf-8") for p in cls.pages}
        cls.parsed = {p.name: parse(p) for p in cls.pages}
        cls.script = {name: "\n".join(p.scripts) for name, p in cls.parsed.items()}

    def test_files_exist_and_are_not_empty(self):
        for page in self.pages:
            self.assertTrue(page.is_file(), page)
            self.assertGreater(page.stat().st_size, 1000, page)

    def test_html_parses_with_balanced_tags(self):
        for name, parser in self.parsed.items():
            self.assertEqual(parser.problems, [], name)
            self.assertEqual(parser.stack, [], "%s: tags left open" % name)

    def test_doctype_lang_charset_and_title(self):
        for name, parser in self.parsed.items():
            self.assertEqual(parser.doctype.lower(), "doctype html", name)
            self.assertTrue(find(parser, "html", lang="*"), name)
            self.assertTrue(find(parser, "meta", charset="utf-8"), name)
            self.assertIn("<title>", self.text[name])

    def test_viewport_meta(self):
        for name, parser in self.parsed.items():
            viewports = find(parser, "meta", name="viewport")
            self.assertEqual(len(viewports), 1, name)
            self.assertIn("width=device-width", viewports[0]["content"] or "", name)
            self.assertIn("initial-scale=1", viewports[0]["content"] or "", name)

    def test_no_html_injection_apis(self):
        banned = [r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(",
                  r"new\s+Function\s*\(", r"setTimeout\s*\(\s*['\"]", r"setInterval\s*\(\s*['\"]", r"DOMParser",
                  r"createContextualFragment"]
        for name, text in self.text.items():
            for pattern in banned:
                self.assertIsNone(re.search(pattern, text), "%s uses %s" % (name, pattern))

    def test_text_goes_in_with_textcontent(self):
        for name, script in self.script.items():
            self.assertIn("textContent", script, name)
            self.assertIn("createElement", script, name)
            self.assertGreaterEqual(script.count("textContent"), 8, name)

    def test_no_inline_event_handler_attributes(self):
        for name, parser in self.parsed.items():
            for tag, attrs in parser.tags:
                handlers = [a for a in attrs if a.startswith("on")]
                self.assertEqual(handlers, [], "%s <%s> has %s" % (name, tag, handlers))

    def test_no_external_scripts_or_styles(self):
        for name, parser in self.parsed.items():
            self.assertEqual(find(parser, "script", src="*"), [], "%s loads a script file" % name)
            for attrs in find(parser, "link", href="*"):
                self.assertNotRegex(attrs["href"] or "", r"^(https?:)?//", "%s loads %s" % (name, attrs["href"]))
            self.assertNotRegex("\n".join(parser.styles), r"@import|url\(\s*['\"]?(https?:)?//", name)
            self.assertNotRegex(self.script[name], r"import\s*\(|importScripts", name)

    def test_fetch_is_same_origin_with_credentials(self):
        for name, script in self.script.items():
            calls = re.findall(r"\bfetch\(\s*([^,)]+)", script)
            self.assertTrue(calls, name)
            for target in calls:
                self.assertNotRegex(target, r"https?:", "%s fetches another site: %s" % (name, target))
            self.assertEqual(script.count("fetch("), script.count("credentials: 'same-origin'"),
                             "%s: every fetch must send credentials: 'same-origin'" % name)

    def test_dark_mode_focus_ring_and_utf8(self):
        for name, parser in self.parsed.items():
            css = "\n".join(parser.styles)
            self.assertIn("prefers-color-scheme: dark", css, name)
            self.assertIn(":focus-visible", css, name)
            self.assertTrue(find(parser, "meta", name="color-scheme"), name)

    def test_no_em_dashes_in_page_text(self):
        # SPEC.md section 0: user-facing English has no em dashes.
        for name, text in self.text.items():
            self.assertNotIn("—", text, name)

    def test_every_button_is_a_real_button_with_type(self):
        for name, parser in self.parsed.items():
            for attrs in find(parser, "button"):
                self.assertIn(attrs.get("type"), ("button", "submit"), name)
            self.assertNotRegex(self.text[name], r"role=['\"]button['\"]", name)


class ChatPage(unittest.TestCase):
    """The tester's chat: SPEC 2.1 'HTTP shapes' and section 8."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = CHAT.read_text(encoding="utf-8")
        cls.parser = parse(CHAT)
        cls.script = "\n".join(cls.parser.scripts)

    def test_uses_exact_api_paths(self):
        for path in ("'/api/state'", "'/api/send'", "'/api/answer'"):
            self.assertIn(path, self.script)
        self.assertNotIn("/api/admin", self.script)
        self.assertNotIn("/api/reset", self.script)

    def test_send_and_answer_bodies_use_spec_field_names(self):
        self.assertRegex(self.script, r"postJSON\('/api/send',\s*\{\s*text:")
        self.assertRegex(self.script, r"postJSON\('/api/answer',\s*\{\s*commitment_id:\s*\w+,\s*action:")
        self.assertIn("method: 'POST'", self.script)
        self.assertIn("'Content-Type': 'application/json'", self.script)
        self.assertIn("JSON.stringify(data)", self.script)

    def test_reads_state_field_names_from_spec(self):
        for field in ("state.messages", "state.open", "state.commitments", "m.role", "m.text", "m.kind", "m.lang",
                      "m.created_at", "m.commitment_id", "nudge_at_local", "nudge_at", "c.status", "c.title"):
            self.assertIn(field, self.script, field)
        self.assertIn("'scheduled'", self.script)
        self.assertIn("'nudge'", self.script)
        self.assertRegex(self.script, r"open\.includes\(m\.id\)")       # buttons follow the ids in state.open

    def test_buttons_only_for_actions_done_and_later(self):
        self.assertIn("['done', 'later']", self.script)

    def test_polls_every_one_and_a_half_seconds(self):
        self.assertRegex(self.script, r"POLL_MS\s*=\s*1500\b")
        self.assertIn("setTimeout(poll, POLL_MS)", self.script)

    def test_conflict_409_just_refreshes(self):
        self.assertIn("response.status !== 409", self.script)

    def test_error_messages_for_http_errors(self):
        self.assertIn("status === 429", self.script)
        self.assertIn("'Too many messages, wait a minute'", self.script)
        self.assertIn("'Demasiados mensajes, espera un minuto'", self.script)
        for code in ("rate_limited", "too_large", "forbidden"):
            self.assertIn("'%s'" % code, self.script)
        self.assertIn("status >= 500", self.script)
        error = find(self.parser, "span", id="error")
        self.assertEqual(error[0].get("role"), "alert")

    def test_message_list_is_a_polite_live_region(self):
        logs = find(self.parser, "main", id="log")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].get("aria-live"), "polite")
        self.assertEqual(logs[0].get("role"), "log")

    def test_text_box_has_label_maxlength_and_counter(self):
        inputs = find(self.parser, "input", id="text")
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0].get("maxlength"), "500")
        self.assertTrue(find(self.parser, "label", **{"for": "text"}))
        self.assertIn("counter", inputs[0].get("aria-describedby") or "")
        self.assertRegex(self.script, r"MAX_LENGTH\s*=\s*500\b")
        self.assertTrue(find(self.parser, "span", id="counter"))
        self.assertEqual(find(self.parser, "button", id="send")[0].get("type"), "submit")   # Enter submits the form

    def test_send_is_disabled_while_sending(self):
        self.assertIn("sendButton.disabled = true", self.script)
        self.assertIn("sendButton.disabled = false", self.script)

    def test_both_languages_have_every_ui_string(self):
        words = re.search(r"const WORDS = \{\n  en: \{(.*?)\n\};", self.script, re.S).group(1)
        en, es = words.split("\n  es: {")
        # a key is a name after "{", "," or the start of a line, followed by ":" and a value
        keys = lambda block: set(re.findall(r"(?:^|[{,])\s*(\w+):\s*['\[{]", block, re.M))
        self.assertGreaterEqual(len(keys(en)), 20)
        self.assertEqual(keys(en), keys(es))
        for spanish in ("'Hecho'", "'Más tarde'", "'Enviar'", "'Escribe un compromiso…'", "'Próximos'"):
            self.assertIn(spanish, es)
        for english in ("'Done'", "'Later'", "'Send'", "'Text a commitment…'", "'Upcoming'"):
            self.assertIn(english, en)

    def test_language_follows_latest_wazy_message(self):
        body = re.search(r"function pickLanguage\(messages\) \{(.*?)\n\}", self.script, re.S).group(1)
        self.assertIn("messages.length - 1", body)            # walks back from the newest message
        self.assertIn("role !== 'user'", body)                # only Wazy's messages count
        self.assertIn("document.documentElement.lang = lang", self.script)

    def test_wazy_and_person_on_different_sides(self):
        css = "\n".join(self.parser.styles)
        self.assertRegex(css, r"\.msg\.user\s*\{[^}]*align-self:flex-end")
        self.assertRegex(css, r"\.msg\.bot\s*\{[^}]*align-self:flex-start")

    def test_upcoming_list_present(self):
        self.assertTrue(find(self.parser, "ul", id="upcoming-list"))
        self.assertTrue(find(self.parser, "section", id="upcoming", aria_labelledby="upcoming-title"))

    def test_works_on_phone_and_desktop(self):
        css = "\n".join(self.parser.styles)
        self.assertIn("@media (min-width: 900px)", css)
        self.assertIn("100dvh", css)
        self.assertIn("min-height:44px", css)                  # touch targets


class DemoPage(unittest.TestCase):
    """The presenter view: SPEC 2 admin_state() and section 8."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DEMO.read_text(encoding="utf-8")
        cls.parser = parse(DEMO)
        cls.script = "\n".join(cls.parser.scripts)

    def test_uses_admin_api_paths(self):
        for path in ("'/api/admin/state'", "'/api/link'", "'/api/reset'"):
            self.assertIn(path, self.script)
        self.assertNotIn("'/api/send'", self.script)
        self.assertNotIn("'/api/answer'", self.script)

    def test_reset_asks_first_and_posts_json(self):
        reset = self.script[self.script.index("$('reset').addEventListener"):]
        reset = reset[:reset.index("});") + 3]
        self.assertIn("window.confirm(", reset)
        self.assertLess(reset.index("window.confirm("), reset.index("fetch('/api/reset'"))
        self.assertIn("method: 'POST'", reset)
        self.assertIn("'Content-Type': 'application/json'", reset)

    def test_reads_admin_state_field_names_from_spec(self):
        for field in ("state.focus_user", "state.users", "state.messages", "state.open", "state.commitments",
                      "state.outcomes", "state.stats", "state.learning_log", "state.reader_misses"):
            self.assertIn(field, self.script, field)
        for field in ("time_source", "nudge_at_local", "later_count", "raw_text", "reader", "seconds_to_respond",
                      "nudged_at", "outcome", "old_hour", "new_hour", "event", "original", "problem", "reply",
                      "learned_hour", "last_seen_at", "opted_out", "tz", "done_rate", "nudges_sent", "no_reply"):
            self.assertRegex(self.script, r"\.%s\b" % field, field)

    def test_labels_for_every_status_code(self):
        for code in ("scheduled", "nudged", "done", "stopped", "no_reply", "cancelled"):
            self.assertRegex(self.script, r"const STATUS = \{[^}]*\b%s:" % code)
        for code in ("pending", "done", "later", "stop", "no_reply", "cancelled"):
            self.assertRegex(self.script, r"const OUTCOME = \{[^}]*\b%s:" % code)
        for code in ("stated", "default", "learned"):
            self.assertRegex(self.script, r"const SOURCE = \{[^}]*\b%s:" % code)

    def test_says_text_is_shown_only_via_textcontent(self):
        self.assertRegex(self.script, r"only via textContent")

    def test_live_conversation_and_reader_misses_sections(self):
        convo = find(self.parser, "div", id="convo")
        self.assertEqual(convo[0].get("aria-live"), "polite")
        self.assertIn("What Wazy could not read", self.text)
        self.assertTrue(find(self.parser, "div", id="misses"))
        for box in ("commitments", "outcomes", "learning", "users", "kpis"):
            self.assertTrue(find(self.parser, "div", id=box), box)

    def test_link_box_and_inline_qr(self):
        self.assertTrue(find(self.parser, "a", id="link"))
        self.assertIn("const QR = (() =>", self.script)       # the QR maker is in the page, no library
        self.assertIn("createElementNS", self.script)
        self.assertNotIn("QRCode(", self.script)              # the v1 external library is gone

    def test_polls_every_one_and_a_half_seconds(self):
        self.assertRegex(self.script, r"POLL_MS\s*=\s*1500\b")

    def test_phone_numbers_are_partly_hidden(self):
        body = re.search(r"function shortWho\(id\) \{(.*?)\n\}", self.script, re.S).group(1)
        self.assertIn("'••••'", body)


if __name__ == "__main__":
    unittest.main()
