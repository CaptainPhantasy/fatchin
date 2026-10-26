"""Stable asset/API contracts for the dependency-free interface.

Task field labels, skipped item steps, live draft decisions and no-check
warnings are verified through actual DOM and request behavior in
 gui-browser-fixture.mjs and gui-learning.mjs. Do not duplicate those checks
with assertions about a particular JavaScript expression or exact help text.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'web' / 'app.js').read_text()
INDEX = (ROOT / 'web' / 'index.html').read_text()
FIXTURE = (ROOT / 'tests' / 'gui-browser-fixture.mjs').read_text()
EDGES = (ROOT / 'tests' / 'gui-browser-edges.mjs').read_text()

class TaskCardTests(unittest.TestCase):
    def test_cards_come_from_the_worker_catalog_not_from_the_page(self):
        self.assertEqual(APP.count('/api/task-types'), 1)
        self.assertIn('taskTypes()', APP)
        self.assertIn('choice-card${w.kind === item.id ?', APP)
        for hard_coded in ('value="fix-pr"', 'value="implement-issue"'):
            self.assertNotIn(hard_coded, APP)

    def test_task_list_is_cached_for_the_page_not_refetched_per_render(self):
        self.assertIn('A.taskTypesRequest', APP)
        self.assertIn('if (A.taskTypes) return Promise.resolve(A.taskTypes);', APP)
        self.assertIn('loadTaskTypes().catch(() => {})', APP)





    def test_a_successful_hint_is_shown_without_blocking_the_reply(self):
        self.assertIn('noticeHint(result.hint);', APP)
        self.assertIn('if (A.hintShown.text === value && now - A.hintShown.at < 60000) return;', APP)
        self.assertIn('failure.hint = typeof result.hint === "string" ? result.hint : "";', APP)

    def test_page_adds_no_inline_script_for_the_content_security_policy(self):
        self.assertNotIn('<script>', INDEX)
        self.assertIn('<script src="/static/app.js" defer></script>', INDEX)

    def test_both_browser_fixtures_serve_every_new_endpoint(self):
        for runner, text in (('gui-browser-fixture.mjs', FIXTURE), ('gui-browser-edges.mjs', EDGES)):
            with self.subTest(runner=runner):
                for endpoint in ('"/api/task-types"', '"/api/authority"',
                                 '"/api/fence"', '"/api/digest"',
                                 'path.endsWith("/draft-action")'):
                    self.assertIn(endpoint, text)
                self.assertIn('Posted. The words are now on GitHub.', text)


if __name__ == '__main__':
    unittest.main()
