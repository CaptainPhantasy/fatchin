"""Bounded, read-only repository exploration and source-checked shortcuts.

Only the injected GitHub client's fixed GET endpoints are used. Provider links
are returned for a deliberate browser launch; this module never fetches them.
"""
from __future__ import annotations

import base64
import binascii
import codecs
import re
import shlex
from urllib.parse import quote, urlencode

VERIFIED = "2026-10-25"
MAX_PREVIEW = 64 * 1024
MAX_PATCH = 4096
MAX_COMPARE_FILES = 100
_SHORTCUT_SOURCE = "https://docs.github.com/en/get-started/accessibility/keyboard-shortcuts"
_FILE_SOURCE = "https://docs.github.com/en/repositories/working-with-files/using-files/viewing-and-understanding-files"
_ARCHIVE_SOURCE = "https://docs.github.com/en/repositories/working-with-files/using-files/downloading-source-code-archives"
_COMPARE_SOURCE = "https://docs.github.com/en/pull-requests/reference/branches#comparing-branches-in-pull-requests"
_SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")


def catalog() -> dict:
    """Local help only: no GitHub, model or third-party request."""
    entries = [
        ("diagram", "Architecture diagram", "See an AI sketch of how a project fits together.",
         "Start here when the folders are unfamiliar.", "Open GitDiagram for the public project.",
         "It samples the tree, README and some source. Check its conclusions; coverage, speed and free usage are not guaranteed.",
         "https://gitdiagram.com/privacy"),
        ("ingest", "Text for an AI", "Collect a summary, folder list and selected file contents.",
         "Prepare code context for a question.", "Open GitIngest, check its filters, then Copy all or Download.",
         "Filters omit files; the website currently starts with a 50 kB per-file limit. A digest is a snapshot, not a live connection.",
         "https://gitingest.com/"),
        ("editor", "Edit in your browser", "Browse, search and edit with GitHub's browser editor.",
         "Make an edit without installing a local editor.", "Open github.dev or press . on GitHub.",
         "Requires GitHub sign-in. No terminal or code runtime; only web-compatible extensions. Commit to save outside browser storage.",
         "https://docs.github.com/en/codespaces/the-githubdev-web-based-editor"),
        ("viewer", "Read in a familiar editor", "Browse a public project with GitHub1s.",
         "Read code using a VS Code-style layout.", "Open GitHub1s for the public project.",
         "A separate service from github.dev. Remote files are read-only; search and access have upstream limits.",
         "https://github.com/conwnet/github1s"),
        ("wiki", "Project explanation", "Read an AI-generated wiki and ask about the code.",
         "Find a guided introduction when documentation is sparse.", "Open DeepWiki; request indexing if the project is missing.",
         "Public documentation and Q&A have a free option. Indexing takes time and explanations can miss details or become stale.",
         "https://docs.devin.ai/work-with-devin/deepwiki"),
        ("mcp", "Connect an AI reader", "Let a compatible AI client query hosted project documentation and code.",
         "Ask follow-up questions without pasting a whole digest.", "Choose your client and copy its GitMCP setup below; merge it with existing settings.",
         "Read tools, not a writable clone. Client setup differs; Claude Desktop's documented bridge needs Node/npx. Availability and freshness vary.",
         "https://github.com/idosal/git-mcp"),
        ("source", "Markdown source", "See Markdown punctuation inside GitHub's file page.",
         "Inspect a document's formatting.", "Select a Markdown file, then open Source (?plain=1).",
         "This is GitHub's interface, not a raw-file download.",
         "https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-a-permanent-link-to-a-code-snippet"),
        ("raw", "Raw file", "Open a file without GitHub's page styling.",
         "Read or save the file itself.", "Select a file and open Raw.",
         "Private files still need GitHub access. FatChin never forwards credentials to the browser.", _FILE_SOURCE),
        ("permalink", "Stable file link", "Link to the exact version you are reading.",
         "Share evidence that should not follow later branch edits.", "Select a file and copy Permalink, or press y on GitHub.",
         "Pinned to a commit; it still requires repository access and the commit to remain available.",
         "https://docs.github.com/en/repositories/working-with-files/using-files/getting-permanent-links-to-files"),
        ("blame", "Who changed each line", "Trace a file's lines to earlier commits.",
         "Find the change that introduced a line.", "Select a file and open Line history, or press b on GitHub.",
         "Shows recorded authorship, not the reason a decision was made.", _FILE_SOURCE),
        ("patch", "Commit patch", "Read a commit's changes as a text patch with commit details.",
         "Inspect or share a change.", "Open the selected commit's .patch link.",
         "Private repositories require access. Merge patches can include a series of commits. Reading a patch does not apply it.",
         "https://docs.github.com/en/rest/commits/commits#get-a-commit"),
        ("diff", "Commit differences", "Read the changed lines from one commit.",
         "Inspect the text changes without the full GitHub page.", "Open the selected commit's .diff link.",
         "Large or binary differences may not be available as text. Private repositories require access.",
         "https://docs.github.com/en/rest/commits/commits#get-a-commit"),
        ("compare", "Compare versions", "See changes introduced since two versions shared an ancestor.",
         "Review a branch's work before combining it.", "Choose Base and Head; open the comparison.",
         "Three dots (...) compare the common ancestor to Head. Two dots (..) compare the two endpoints directly.", _COMPARE_SOURCE),
        ("archive", "Download this version", "Download a ZIP snapshot of the selected commit.",
         "Read files locally without cloning Git history.", "Open Download ZIP. FatChin uses the selected commit, not an assumed main branch.",
         "No Git history or submodule contents. Large-file (LFS) inclusion depends on repository settings.", _ARCHIVE_SOURCE),
        ("avatar", "Project owner's picture", "Open the repository owner's GitHub profile picture.",
         "Identify the owner visually.", "Open Owner picture; the profile URL ends in .png.",
         "This identifies a profile, not proof of project ownership or trust.",
         "https://docs.github.com/en/rest/users/users#get-a-user"),
    ]
    tools = [dict(zip(("id", "title", "purpose", "when", "how", "limits", "source"), entry), verified=VERIFIED)
             for entry in entries]
    shortcuts = [
        {"key": key, "action": action, "where": where}
        for key, action, where in [
            (".", "Open GitHub's browser editor", "Repository or pull request; GitHub sign-in required"),
            ("t", "Find a file by name", "Repository code browsing"),
            ("y", "Pin the file URL to its commit", "File code view"),
            ("b", "Show who changed each line", "File code view"),
            ("l", "Jump to a line number", "File code view"),
            ("?", "Show this page's shortcuts", "GitHub pages; available keys depend on context"),
        ]
    ]
    return {"tools": tools, "shortcuts": shortcuts,
            "clients": [{"id": key, "label": label} for key, label in
                        (("cursor", "Cursor"), ("windsurf", "Windsurf"),
                         ("vscode", "VS Code"), ("claude_desktop", "Claude Desktop (Node/npx bridge)"))],
            "verified": VERIFIED, "shortcut_source": _SHORTCUT_SOURCE,
            "shortcut_note": "Use GitHub shortcuts outside text inputs. Character shortcuts can be disabled in accessibility settings.",
            "notice": "External tools open separately. Free options do not guarantee unlimited use or complete, current AI explanations."}


def _repo(value: str) -> str:
    if not isinstance(value, str) or not _REPO.fullmatch(value) or value.rsplit("/", 1)[1] in (".", ".."):
        raise ValueError("Choose a GitHub project using owner/repository, without a URL.")
    return value


def _ref(value: str, label: str = "version") -> str:
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError(f"Choose a valid {label} (branch, tag or commit).")
    if value and (any(ord(c) < 33 or ord(c) == 127 for c in value) or
                  any(c in value for c in "~^:?*[\\") or
                  any(c in value for c in ("..", "//", "@{")) or
                  value.startswith(("-", "/", ".")) or value.endswith(("/", ".")) or
                  any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise ValueError(f"Choose a valid {label} (branch, tag or commit).")
    return value


def _path(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 1024 or len(value.split("/")) > 32 or
        "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value) or
        (value and any(part in ("", ".", "..") for part in value.split("/")))):
        raise ValueError("Choose a file or folder inside the project, without leading slashes or .. segments.")
    return value


def _sha(value) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("GitHub returned an invalid commit ID. Reload the project before continuing.")
    return value.lower()


def _mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"GitHub returned an unreadable {label}. Try loading it again.")
    return value


def _list(value, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"GitHub returned an unreadable {label}. Try loading it again.")
    return value


def _text(value, limit: int = 2000) -> str:
    return str(value or "")[:limit]


def _history(entry: dict) -> dict:
    entry = _mapping(entry, "commit")
    details = _mapping(entry.get("commit"), "commit details")
    author = _mapping(details.get("author") or {}, "commit author")
    return {"sha": _sha(entry.get("sha")), "message": _text(details.get("message")),
            "author": _text(author.get("name"), 200), "date": _text(author.get("date"), 64)}


def _templates(url: str | None) -> dict:
    if not url:
        return dict.fromkeys(("cursor", "windsurf", "vscode", "claude_desktop"))
    return {
        "cursor": {"mcpServers": {"repo-docs": {"url": url}}},
        "windsurf": {"mcpServers": {"repo-docs": {"serverUrl": url}}},
        "vscode": {"servers": {"repo-docs": {"type": "sse", "url": url}}},
        "claude_desktop": {"mcpServers": {"repo-docs": {"command": "npx", "args": ["mcp-remote", url]}}},
    }


class Explorer:
    """Repository evidence, pinned to a resolved commit and safe to display."""

    def __init__(self, gh):
        self.gh = gh

    catalog = staticmethod(catalog)

    def inspect(self, repo: str, view: str = "tree", ref: str = "", path: str = "",
                base: str = "", head: str = "", commit: str = "") -> dict:
        repo, ref, path = _repo(repo), _ref(ref), _path(path)
        base, head, commit = _ref(base, "base version"), _ref(head, "head version"), _ref(commit, "commit")
        if view not in ("tree", "file", "history", "compare"):
            raise ValueError("Choose Files, File, History or Compare.")
        if view == "file" and not path:
            raise ValueError("Choose a file before opening its contents.")
        if view == "compare" and (not base or not head):
            raise ValueError("Choose both Base and Head to compare versions.")
        receipts = []

        def get(endpoint, explanation, equivalent="", params=None):
            # Query values are encoded by the existing client. No endpoint or
            # HTTP method comes from an API response or an arbitrary URL.
            result = self.gh.rest("GET", endpoint, body=params, paginate=False)
            actual = endpoint + (("?" + urlencode(params, quote_via=quote)) if params else "")
            receipts.append({"command": shlex.join(["gh", "api", "--method", "GET", actual]),
                             "explanation": explanation, "git_equivalent": equivalent})
            return result

        endpoint = "repos/" + repo
        metadata = _mapping(get(endpoint, "Read the project's visibility and default branch."), "project details")
        private = metadata.get("private") is not False
        # visibility must agree if GitHub includes it. Unknown never means public.
        if metadata.get("visibility") not in (None, "public"):
            private = True
        default = _ref(metadata.get("default_branch") or "", "default branch")
        selected = commit or ref or default
        branches_raw = _list(get(endpoint + "/branches", "List up to 100 branches.",
                                 "git branch --remotes", {"per_page": 100, "page": 1}), "branch list")
        branches_raw = [_mapping(item, "branch") for item in branches_raw[:100]]
        branches = [{"name": _ref(item.get("name", ""), "branch"),
                     "sha": _sha(_mapping(item.get("commit"), "branch commit").get("sha"))}
                    for item in branches_raw]
        truncated = len(branches_raw) >= 100
        sha = ""
        size = metadata.get("size")
        empty = not branches and type(size) is int and size == 0 and not ref and not commit
        if selected and not empty:
            resolved = _mapping(get(endpoint + "/commits/" + quote(selected, safe=""),
                                    "Resolve the selected version to an exact commit.",
                                    shlex.join(["git", "rev-parse", selected + "^{commit}"]),
                                    {"per_page": 1}), "selected commit")
            sha = _sha(resolved.get("sha"))
        elif branches:
            raise ValueError("GitHub did not identify a default branch. Choose a branch and try again.")

        github = "https://github.com/" + repo
        links = dict.fromkeys(("github", "editor", "viewer", "diagram", "ingest", "wiki", "mcp", "source", "raw", "permalink", "blame", "patch", "diff", "compare", "archive", "avatar"))
        links.update(github=github, editor="https://github.dev/" + repo,
                     avatar="https://github.com/" + repo.split("/")[0] + ".png")
        if not private:
            links.update(viewer="https://github1s.com/" + repo, diagram="https://gitdiagram.com/" + repo,
                         ingest="https://gitingest.com/" + repo, wiki="https://deepwiki.com/" + repo,
                         mcp="https://gitmcp.io/" + repo)
        if sha:
            links.update(patch=f"{github}/commit/{sha}.patch", diff=f"{github}/commit/{sha}.diff",
                         archive=f"{github}/archive/{sha}.zip")
            if path:
                blob = f"{github}/blob/{sha}/{quote(path, safe='/')}"
                links.update(permalink=blob, source=blob + "?plain=1",
                             raw=f"{github}/raw/{sha}/{quote(path, safe='/')}",
                             blame=f"{github}/blame/{sha}/{quote(path, safe='/')}")

        result = {"repository": {"full_name": repo, "private": private, "default_branch": default,
                                  "ref": selected, "sha": sha},
                  "branches": branches, "items": [], "file": None, "history": [], "comparison": None,
                  "links": links, "mcp": _templates(links["mcp"]), "receipts": receipts, "truncated": truncated}
        if not sha:
            # A repository with no default ref and no branches has no files yet.
            return result
        if view in ("tree", "file"):
            contents = get(endpoint + "/contents" + ("/" + quote(path, safe="/") if path else ""),
                           "Read this folder or file at the pinned commit.",
                           shlex.join(["git", "ls-tree", sha, "--", path or "."]) if view == "tree" else
                           shlex.join(["git", "show", sha + ":" + path]), {"ref": sha})
            if isinstance(contents, list):
                if view == "file":
                    raise ValueError("This is a folder. Choose a file to read its contents.")
                items = []
                for item in contents[:1000]:
                    item = _mapping(item, "folder entry")
                    item_path = _path(item.get("path", ""))
                    kind = "submodule" if item.get("submodule_git_url") else item.get("type")
                    if kind not in ("file", "dir", "symlink", "submodule"):
                        kind = "unknown"
                    items.append({"name": _text(item.get("name"), 255), "path": item_path, "type": kind})
                result["items"] = sorted(items, key=lambda item: (item["type"] != "dir", item["name"].casefold()))
                result["truncated"] |= len(contents) >= 1000
                for key in ("source", "raw", "permalink", "blame"):
                    links[key] = None
            else:
                contents = _mapping(contents, "file")
                if contents.get("submodule_git_url") or contents.get("type") != "file":
                    raise ValueError("This entry is a link or submodule. Open it on GitHub to inspect its target.")
                size = contents.get("size")
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise ValueError("GitHub returned an unreadable file size. Open the file on GitHub instead.")
                encoded = contents.get("content", "")
                if not isinstance(encoded, str):
                    raise ValueError("GitHub returned unreadable file contents.")
                # Contents API's default JSON includes at most 1 MB per file.
                # Oversize/unsupported payloads never trigger a raw-URL fetch.
                cut = size > MAX_PREVIEW or len(encoded) > (MAX_PREVIEW * 4 // 3 + 2048)
                if contents.get("encoding") != "base64":
                    if size <= MAX_PREVIEW:
                        raise ValueError("GitHub could not provide this file as text. Use Raw on GitHub instead.")
                    result["file"] = {"path": path, "text": "", "truncated": True, "binary": False}
                else:
                    # Decode only enough base64 to show the bounded prefix.
                    compact = "".join(encoded[:(MAX_PREVIEW * 4 // 3 + 4096)].split())
                    max_encoded = ((MAX_PREVIEW + 3) // 3) * 4
                    compact = compact[:max_encoded]
                    try:
                        data = base64.b64decode(compact, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise ValueError("GitHub returned invalid file encoding. Reload the file.") from exc
                    cut |= len(data) > MAX_PREVIEW
                    data = data[:MAX_PREVIEW]
                    try:
                        text = codecs.getincrementaldecoder("utf-8")().decode(data, final=not cut)
                        binary = "\x00" in text
                    except UnicodeDecodeError:
                        text, binary = "", True
                    result["file"] = {"path": path, "text": "" if binary else text,
                                      "truncated": cut, "binary": binary}
                result["truncated"] |= result["file"]["truncated"]
        elif view == "history":
            params = {"sha": sha, "per_page": 100, "page": 1}
            if path:
                params["path"] = path
            entries = _list(get(endpoint + "/commits", "Read up to 100 commits from this version.",
                                shlex.join(["git", "log", "-100", sha, "--"] + ([path] if path else [])), params), "history")
            result["history"] = [_history(item) for item in entries[:100]]
            result["truncated"] |= len(entries) >= 100
        else:
            def resolve(value):
                return _sha(_mapping(get(endpoint + "/commits/" + quote(value, safe=""),
                                         "Pin a comparison endpoint to its exact commit.",
                                         shlex.join(["git", "rev-parse", value + "^{commit}"]),
                                         {"per_page": 1}), "comparison commit").get("sha"))
            base_sha, head_sha = resolve(base), resolve(head)
            comparison = _mapping(get(endpoint + f"/compare/{base_sha}...{head_sha}",
                                      "Read changes from the common ancestor to Head; one page only.",
                                      shlex.join(["git", "diff", base_sha + "..." + head_sha]),
                                      {"per_page": 100, "page": 1}), "comparison")
            entries = _list(comparison.get("commits"), "comparison commits")
            files = _list(comparison.get("files"), "comparison files")
            trimmed = len(entries) >= 100 or len(files) >= MAX_COMPARE_FILES
            total = comparison.get("total_commits", len(entries))
            if isinstance(total, int):
                trimmed |= total > len(entries)
            kept = []
            for item in files[:MAX_COMPARE_FILES]:
                item = _mapping(item, "changed file")
                patch = _text(item.get("patch"), MAX_PATCH)
                patch_cut = len(str(item.get("patch") or "")) > MAX_PATCH
                kept.append({"path": _path(item.get("filename", "")), "status": _text(item.get("status"), 32),
                             "additions": item.get("additions", 0), "deletions": item.get("deletions", 0),
                             "patch": patch, "truncated": patch_cut})
                trimmed |= patch_cut
            result["comparison"] = {"base": base_sha, "head": head_sha,
                                    "merge_base": _sha(_mapping(comparison.get("merge_base_commit"), "common ancestor").get("sha")),
                                    "files": kept, "commits": [_history(item) for item in entries[:100]], "truncated": trimmed}
            links["compare"] = f"{github}/compare/{base_sha}...{head_sha}"
            result["truncated"] |= trimmed
        return result
