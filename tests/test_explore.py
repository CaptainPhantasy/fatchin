"""Explorer regressions: public/private boundaries and bounded real evidence."""
import base64
import copy
import unittest

from ghagent.explore import Explorer, MAX_PREVIEW, catalog
from ghagent.github_client import NotFound, RateLimited


A, B, C = "a" * 40, "b" * 40, "c" * 40


def commit(sha=A, message="Initial change"):
    return {"sha": sha, "commit": {"message": message, "author": {"name": "Example", "date": "2026-10-25T00:00:00Z"}}}


def file(data=b"Hello\n", path="README.md", **changes):
    result = {"path": path, "name": path.rsplit("/", 1)[-1], "type": "file", "size": len(data),
              "encoding": "base64", "content": base64.encodebytes(data).decode("ascii")}
    result.update(changes)
    return result


class GitHub:
    def __init__(self, private=False, **metadata):
        self.metadata = {"private": private, "default_branch": "trunk", "size": 1, **metadata}
        self.branches = [{"name": "trunk", "commit": {"sha": A}}]
        self.contents = [{"name": "README.md", "path": "README.md", "type": "file"},
                         {"name": "src", "path": "src", "type": "dir"}]
        self.history = [commit(A), commit(B)]
        self.comparison = {"commits": [commit(A)], "files": [{"filename": "README.md", "status": "modified", "additions": 1,
                            "deletions": 0, "patch": "@@ -1 +1 @@\n-before\n+after"}],
                           "merge_base_commit": {"sha": B}, "total_commits": 1}
        self.calls = []
        self.error = None

    def rest(self, method, path, body=None, paginate=False):
        self.calls.append((method, path, body, paginate))
        if self.error:
            raise self.error
        if method != "GET" or not path.startswith("repos/") or paginate:
            raise AssertionError("Only fixed, single-page GitHub GET calls are allowed")
        if path.endswith("/branches"):
            return copy.deepcopy(self.branches)
        if "/contents" in path:
            return copy.deepcopy(self.contents)
        if "/compare/" in path:
            return copy.deepcopy(self.comparison)
        if path.endswith("/commits"):
            return copy.deepcopy(self.history)
        if "/commits/" in path:
            ref = path.rsplit("/", 1)[-1]
            return commit(B if ref in (B, "old") else C if ref == C else A)
        return copy.deepcopy(self.metadata)


class ExplorerTests(unittest.TestCase):
    def test_catalog_complete_and_local(self):
        gh = GitHub()
        result = Explorer(gh).catalog()
        self.assertEqual(gh.calls, [])
        self.assertEqual(result["verified"], "2026-10-25")
        self.assertEqual(set(item["id"] for item in result["tools"]),
                         {"diagram", "ingest", "editor", "viewer", "wiki", "mcp", "source", "raw", "permalink", "blame", "patch", "diff", "compare", "archive", "avatar"})
        self.assertEqual(set(item["key"] for item in result["shortcuts"]), {".", "t", "y", "b", "l", "?"})
        self.assertTrue(all(item["source"].startswith("https://") and item["limits"] for item in result["tools"]))
        result["tools"][0]["title"] = "Modified"
        self.assertNotEqual(catalog()["tools"][0]["title"], "Modified")

    def test_tree_pins_commit_and_uses_actual_default(self):
        gh = GitHub()
        result = Explorer(gh).inspect("example/project")
        self.assertEqual(result["repository"]["ref"], "trunk")
        self.assertEqual(result["repository"]["sha"], A)
        self.assertEqual(result["items"][0]["type"], "dir")
        self.assertEqual(gh.calls[-1][2], {"ref": A})
        self.assertIn("/archive/" + A, result["links"]["archive"])
        self.assertNotIn("main", result["links"]["archive"])
        self.assertEqual(result["links"]["source"], None)
        self.assertTrue(all(item["command"].startswith("gh api --method GET") for item in result["receipts"]))
        self.assertIn("git ls-tree", result["receipts"][-1]["git_equivalent"])

    def test_private_and_unknown_do_not_return_provider_links_or_config(self):
        for metadata in ({"private": True}, {"private": None}, {"private": "false"}, {"private": False, "visibility": "internal"}):
            with self.subTest(metadata=metadata):
                gh = GitHub(**metadata)
                if metadata["private"] is None:
                    gh.metadata.pop("private")
                result = Explorer(gh).inspect("example/project")
                self.assertTrue(result["repository"]["private"])
                for key in ("diagram", "ingest", "wiki", "mcp", "viewer"):
                    self.assertIsNone(result["links"][key])
                self.assertTrue(all(value is None for value in result["mcp"].values()))
                self.assertTrue(result["links"]["github"].startswith("https://github.com/"))
                self.assertTrue(result["links"]["editor"].startswith("https://github.dev/"))

    def test_client_specific_configs(self):
        result = Explorer(GitHub()).inspect("example/project")["mcp"]
        self.assertEqual(result["cursor"]["mcpServers"]["repo-docs"]["url"], "https://gitmcp.io/example/project")
        self.assertIn("serverUrl", result["windsurf"]["mcpServers"]["repo-docs"])
        self.assertEqual(result["vscode"]["servers"]["repo-docs"]["type"], "sse")
        self.assertEqual(result["claude_desktop"]["mcpServers"]["repo-docs"]["command"], "npx")

    def test_unusual_ref_and_path_encoded_and_pinned(self):
        gh = GitHub()
        gh.contents = file("Hello π\n".encode(), "docs/A #%.md")
        result = Explorer(gh).inspect("owner/repo.test", "file", "feature/ui#2", "docs/A #%.md")
        self.assertIn("feature%2Fui%232", gh.calls[2][1])
        self.assertIn("docs/A%20%23%25.md", gh.calls[-1][1])
        self.assertEqual(result["file"]["text"], "Hello π\n")
        self.assertIn("/blob/" + A + "/", result["links"]["permalink"])
        self.assertTrue(result["links"]["source"].endswith("?plain=1"))
        self.assertIn("/raw/", result["links"]["raw"])
        self.assertNotIn("plain=1", result["links"]["raw"])
        self.assertEqual(gh.calls[-1][2], {"ref": A})

    def test_input_rejection_precedes_requests(self):
        invalid = [dict(repo="https://github.com/a/b"), dict(repo="a/b/commits"), dict(repo="a/.."),
                   dict(repo="a/b", ref="../main"), dict(repo="a/b", ref="main?x=1"),
                   dict(repo="a/b", ref="--help"), dict(repo="a/b", ref="a\\b"),
                   dict(repo="a/b", path="/etc/passwd"), dict(repo="a/b", path="a/../b"),
                   dict(repo="a/b", path="a\x00b"), dict(repo="a/b", path="a\\b"),
                   dict(repo="a/b", view="https://elsewhere.test"), dict(repo="a/b", view="file")]
        for params in invalid:
            with self.subTest(params=params):
                gh = GitHub()
                with self.assertRaises(ValueError):
                    Explorer(gh).inspect(**params)
                self.assertEqual(gh.calls, [])

    def test_preview_limit_and_utf8_boundary(self):
        gh = GitHub()
        gh.contents = file(b"x" * (MAX_PREVIEW - 1) + "€ trailing".encode())
        result = Explorer(gh).inspect("a/b", "file", path="README.md")
        self.assertEqual(result["file"]["text"], "x" * (MAX_PREVIEW - 1))
        self.assertFalse(result["file"]["binary"])
        self.assertTrue(result["file"]["truncated"])
        self.assertTrue(result["truncated"])

    def test_binary_and_empty_files(self):
        for data, binary in ((b"", False), (b"a\x00b", True), (b"\xff\xfeabc", True), (b"\xff" + b"a" * MAX_PREVIEW, True)):
            with self.subTest(data=data[:8]):
                gh = GitHub()
                gh.contents = file(data)
                result = Explorer(gh).inspect("a/b", "file", path="README.md")
                self.assertEqual(result["file"]["binary"], binary)
                self.assertEqual(result["file"]["text"], "")

    def test_unavailable_oversize_content_no_followup_fetch(self):
        gh = GitHub()
        gh.contents = file(size=2_000_000, content="", encoding="none", download_url="https://attacker.test/")
        result = Explorer(gh).inspect("a/b", "file", path="README.md")
        self.assertTrue(result["file"]["truncated"])
        self.assertEqual(result["file"]["text"], "")
        self.assertEqual(len(gh.calls), 4)

    def test_file_encoding_failure_is_not_invented_empty_text(self):
        for changes in ({"content": "**invalid**"}, {"size": -1}, {"size": "5"}, {"encoding": "none"}):
            with self.subTest(changes=changes):
                gh = GitHub()
                gh.contents = file(**changes)
                with self.assertRaises(ValueError):
                    Explorer(gh).inspect("a/b", "file", path="README.md")

    def test_empty_repository_without_invented_commit(self):
        gh = GitHub(size=0)
        gh.branches = []
        result = Explorer(gh).inspect("a/b")
        self.assertEqual(result["repository"]["sha"], "")
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["links"]["archive"])
        self.assertEqual(len(gh.calls), 2)

    def test_api_failures_propagate_without_fake_empty_state(self):
        for error in (NotFound("Project not found"), RateLimited("GitHub rate limit hit; retry later")):
            gh = GitHub()
            gh.error = error
            with self.assertRaises(type(error)) as raised:
                Explorer(gh).inspect("a/b")
            self.assertIs(raised.exception, error)

    def test_history_empty_or_first_page(self):
        gh = GitHub()
        gh.history = []
        self.assertEqual(Explorer(gh).inspect("a/b", "history")["history"], [])
        gh.history = [commit(message="item " + str(i)) for i in range(101)]
        result = Explorer(gh).inspect("a/b", "history", path="docs/A.md")
        self.assertEqual(len(result["history"]), 100)
        self.assertTrue(result["truncated"])
        self.assertEqual(gh.calls[-1][2], {"sha": A, "per_page": 100, "page": 1, "path": "docs/A.md"})

    def test_directory_and_branches_limits(self):
        gh = GitHub()
        gh.branches *= 101
        gh.contents *= 501
        result = Explorer(gh).inspect("a/b")
        self.assertEqual(len(result["branches"]), 100)
        self.assertEqual(len(result["items"]), 1000)
        self.assertTrue(result["truncated"])

    def test_compare_pins_both_endpoints_and_explains_merge_base(self):
        gh = GitHub()
        result = Explorer(gh).inspect("a/b", "compare", base="old", head="feature/new")
        self.assertEqual(result["comparison"]["base"], B)
        self.assertEqual(result["comparison"]["head"], A)
        self.assertEqual(result["comparison"]["merge_base"], B)
        self.assertEqual(result["links"]["compare"], "https://github.com/a/b/compare/" + B + "..." + A)
        self.assertIn("common ancestor", result["receipts"][-1]["explanation"])
        self.assertEqual(gh.calls[-1][2], {"per_page": 100, "page": 1})

    def test_compare_limits_and_missing_inputs(self):
        gh = GitHub()
        gh.comparison["files"] *= 101
        gh.comparison["files"][0]["patch"] = "x" * 5000
        gh.comparison["total_commits"] = 150
        result = Explorer(gh).inspect("a/b", "compare", base=B, head=A)
        self.assertEqual(len(result["comparison"]["files"]), 100)
        self.assertEqual(len(result["comparison"]["files"][0]["patch"]), 4096)
        self.assertTrue(result["comparison"]["truncated"])
        with self.assertRaisesRegex(ValueError, "both Base and Head"):
            Explorer(gh).inspect("a/b", "compare")

    def test_payload_errors_and_submodule_not_silently_followed(self):
        for contents in (None, {"type": "symlink"}, file(submodule_git_url="https://elsewhere.test/repo")):
            gh = GitHub()
            gh.contents = contents
            with self.assertRaises(ValueError):
                Explorer(gh).inspect("a/b", "file", path="README.md")
        gh = GitHub()
        gh.branches = [None]
        with self.assertRaises(ValueError):
            Explorer(gh).inspect("a/b")


if __name__ == "__main__":
    unittest.main()
