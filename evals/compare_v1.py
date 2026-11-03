"""Before and after: run the same eval cases against v1 (the MVP shown in class) and against v2.

v1 never said WHY it could not use a time, so it has no reply kinds to compare. What both versions
do have is the end result: which commitments were created, with what title, for what time. This
script scores only that end result, in the same way for both versions:

  * cases whose `expect` checks commitments (count, title, nudge time, status) are scored;
  * reply kinds, reply text, language and reader_misses are ignored (v1 has no equivalent).

v1 is taken straight from git (tag pre-rewrite-2026-11-03) into a temporary folder, so nothing in
the working copy changes. Run from the repository root:

    python3.9 evals/compare_v1.py
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
V1_TAG = "pre-rewrite-2026-11-03"
OUTCOME_KEYS = ("commitment_count", "commitments")


def load_runner() -> Any:
    """evals/run.py is a script, not a package, so it is loaded by path."""
    spec = importlib.util.spec_from_file_location("eval_runner", EVALS_DIR / "run.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_runner"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def unpack_v1(folder: Path) -> None:
    """Copy v1's wazy/ package out of git into `folder`."""
    archive = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", V1_TAG, "wazy"],
                             check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(folder)], input=archive, check=True)


class V1App:
    """Makes v1's single-person Store look like v2's App, just enough for the eval runner.

    v1 stored naive local times, so the clock's local wall time is handed over without a zone.
    """

    def __init__(self, store_class: Any, clock: Any, tz_name: str) -> None:
        self.clock = clock
        self.store = store_class(":memory:", clock=self.local_now)

    def local_now(self) -> Any:
        return self.clock().replace(tzinfo=None)

    def handle(self, user_id: str, text: str, **_: Any) -> List[dict]:
        before = self.newest_id()
        self.store.handle_text(text, now=self.local_now())
        return self.bot_messages_after(before)

    def tick(self) -> int:
        return self.store.run_due(self.local_now())

    def tap(self, user_id: str, commitment_id: int, action: str) -> List[dict]:
        before = self.newest_id()
        self.store.answer_nudge(commitment_id, action, now=self.local_now())
        return self.bot_messages_after(before)

    def state(self, user_id: str) -> dict:
        raw = self.store.state()
        commitments = []
        for row in raw["commitments"]:
            row = dict(row)
            row["nudge_at_local"] = row.get("nudge_at") or ""
            commitments.append(row)
        messages = [dict(m) for m in raw["messages"]]
        return {"messages": messages, "open": raw["open"], "commitments": commitments,
                "reader_misses": raw["reader_misses"]}

    def newest_id(self) -> int:
        return max((m["id"] for m in self.store.state()["messages"]), default=0)

    def bot_messages_after(self, before: int) -> List[dict]:
        out = []
        for m in self.store.state()["messages"]:
            if m["id"] > before and m["role"] == "bot":
                first_welcome = m["text"].startswith("Hi, I'm Wazy")
                out.append({"kind": "welcome" if first_welcome else "text", "text": m["text"], "lang": "en"})
        return out

    def close(self) -> None:
        self.store.close()


def outcome_only(case: Any) -> Optional[Any]:
    """The case with only its end-result expectations, or None if it has none."""
    kept = {key: case.expect[key] for key in OUTCOME_KEYS if key in case.expect}
    if not kept:
        return None
    case.expect = kept
    return case


def score(runner: Any, cases: List[Any]) -> Dict[str, Any]:
    by_category: Dict[str, List[int]] = {}
    passed = 0
    for case in cases:
        result = runner.run_case(case)
        ok = not result.failures
        passed += ok
        totals = by_category.setdefault(case.category, [0, 0])
        totals[0] += 1
        totals[1] += ok
    return {"cases": len(cases), "passed": passed, "by_category": by_category}


def main() -> int:
    runner = load_runner()
    cases, errors = runner.load_cases(runner.DEFAULT_CASES_DIR)
    if errors:
        print(f"{len(errors)} case errors; fix them first (python3.9 evals/run.py)")
        return 2
    scored = [c for c in (outcome_only(case) for case in cases) if c is not None]

    sys.path.insert(0, str(REPO_ROOT))
    runner.APP_FACTORY = None
    after = score(runner, scored)

    with tempfile.TemporaryDirectory() as folder:
        unpack_v1(Path(folder))
        for name in [n for n in sys.modules if n == "wazy" or n.startswith("wazy.")]:
            del sys.modules[name]                   # forget v2 so v1 can be imported under the same name
        sys.path.insert(0, folder)
        from wazy.store import Store                # v1
        runner.APP_FACTORY = lambda clock, tz_name: V1App(Store, clock, tz_name)
        before = score(runner, scored)

    print(f"End-result accuracy on {after['cases']} eval cases that check commitments:")
    print(f"  v1 (class demo): {before['passed']}/{before['cases']} = {100 * before['passed'] / before['cases']:.0f}%")
    print(f"  v2:              {after['passed']}/{after['cases']} = {100 * after['passed'] / after['cases']:.0f}%")
    print()
    print("| Category | Cases | v1 | v2 |")
    print("|---|---|---|---|")
    for category in sorted(after["by_category"]):
        total, ok_after = after["by_category"][category]
        ok_before = before["by_category"][category][1]
        print(f"| {category} | {total} | {ok_before} ({100 * ok_before / total:.0f}%) | "
              f"{ok_after} ({100 * ok_after / total:.0f}%) |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
