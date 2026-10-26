"""No-GitHub-charge trigger guard: preflight before ANY remote write.

The worker runs LOCALLY and never starts Actions runs itself. This module
flags only triggers that OUR push / PR / comment writes could start on the
member's behalf: pull_request*, push, workflow_call chains, and comment
events (issue_comment, pull_request_review*). `schedule` is informational
only — nothing this worker does starts a schedule.

Public-repo standard hosted runners are free, but this installation must not
rely on that: push/pull_request workflows still force NEEDS_REVIEW unless the
caller explicitly allowlists the repo (allow_workflows=True).

Anything uninspectable (403s, org settings, oversized files) becomes a reason
and NEEDS_REVIEW — an area is never silently claimed safe.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote

SAFE = "SAFE_TO_WRITE"
REVIEW = "NEEDS_REVIEW"

MAX_WORKFLOW_FILES = 20
MAX_WORKFLOW_BYTES = 64 * 1024

# Triggers our writes can start regardless of any allowlist: external PR
# triggers (run with repo secrets), reusable-workflow chains, and everything
# fired by comments or review comments/reviews we post.
_BLOCKING_ALWAYS = {
    "pull_request_target", "workflow_call", "issue_comment",
    "pull_request_review", "pull_request_review_comment",
}
# Triggers our push to a PR head / new branch can start; waivable only by an
# explicit repo allowlist.
_BLOCKING_UNLESS_ALLOW = {"pull_request", "push"}
_INFORMATIONAL = {"schedule"}
_KNOWN_TRIGGERS = _BLOCKING_ALWAYS | _BLOCKING_UNLESS_ALLOW | _INFORMATIONAL

_ON_LINE = re.compile(r'''^(\s*)(?:"on"|'on'|on|true)\s*:\s*(.*)$''')
_RUNS_ON = re.compile(r"^\s*runs-on\s*:\s*(.*)$")
_HOSTED_LABEL = re.compile(r"(ubuntu-|macos-|windows-)", re.IGNORECASE)
_STEP_LINE = re.compile(r"\b(run|uses|name)\s*:", re.IGNORECASE)
_STEP_WORD = re.compile(r"\b(publish|package|deploy|pages)\b", re.IGNORECASE)
_LFS_FILTER = re.compile(r"filter\s*=\s*lfs\b")


@dataclass
class TriggerReport:
    verdict: str = SAFE                 # SAFE_TO_WRITE | NEEDS_REVIEW
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def _clip(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _hold(report: TriggerReport, reason: str) -> None:
    if reason not in report.reasons:
        report.reasons.append(reason)


def _is_not_found(exc: Exception) -> bool:
    text = str(exc)
    return "404" in text or "Not Found" in text


def _decode_blob(blob: object) -> str | None:
    """Decode a contents-API file payload to text, or None if unreadable."""
    if isinstance(blob, str):
        return blob
    if not isinstance(blob, dict):
        return None
    content = blob.get("content")
    if not isinstance(content, str):
        return None
    if blob.get("encoding") == "base64":
        try:
            return base64.b64decode(content.replace("\n", "")).decode("utf-8", "replace")
        except Exception:
            return None
    return content


def _scan_triggers(text: str) -> list[str]:
    """Line-based scan of the workflow `on:` block (stdlib only, no YAML)."""
    found: set[str] = set()
    in_on = False
    on_indent = 0
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ON_LINE.match(raw)
        if m and not in_on:
            indent, rest = m.group(1), m.group(2).strip()
            if rest:
                for tok in re.findall(r"[A-Za-z_]+", rest):
                    if tok in _KNOWN_TRIGGERS:
                        found.add(tok)
            else:
                in_on = True
                on_indent = len(m.group(1))
            continue
        if in_on:
            indent = len(raw) - len(raw.lstrip(" "))
            if indent <= on_indent:
                in_on = False
                continue
            token = stripped.split(":", 1)[0].strip().strip("\"'").lstrip("-").strip()
            if token in _KNOWN_TRIGGERS:
                found.add(token)
    return sorted(found)


def _step_flags(text: str) -> list[str]:
    """Publish/package/deploy/pages words on step-ish lines (run/uses/name)."""
    words: set[str] = set()
    for line in text.splitlines():
        if _STEP_LINE.search(line):
            for m in _STEP_WORD.finditer(line):
                words.add(m.group(1).lower())
    return sorted(words)


def _hosted_runners(text: str) -> list[str]:
    """GitHub-hosted runner labels found on runs-on lines."""
    labels: set[str] = set()
    for line in text.splitlines():
        m = _RUNS_ON.match(line)
        if not m:
            continue
        for tok in re.findall(r"[A-Za-z0-9._-]+", m.group(1)):
            if _HOSTED_LABEL.search(tok):
                labels.add(tok)
    return sorted(labels)


def _has_lfs_filter(text: str) -> bool:
    return bool(_LFS_FILTER.search(text))


def _flag_workflow_text(name: str, ref: str, text: str,
                        allow_workflows: bool, report: TriggerReport) -> None:
    trig = _scan_triggers(text)
    report.details.setdefault("workflow_findings", []).append(
        {"workflow": name, "ref": ref, "triggers": trig,
         "steps": _step_flags(text), "hosted_runners": _hosted_runners(text)})
    for t in trig:
        if t in _BLOCKING_ALWAYS:
            _hold(report, f"workflow {name}@{ref} is triggered by {t} — this worker's writes could start it")
        elif t in _BLOCKING_UNLESS_ALLOW and not allow_workflows:
            _hold(report, f"workflow {name}@{ref} is triggered by {t} — our push could start "
                          "GitHub Actions (allowlist this repo to waive)")
        elif t == "schedule":
            report.details.setdefault("schedule_workflows", []).append(f"{name}@{ref}")
    steps = _step_flags(text)
    if steps:
        _hold(report, f"workflow {name}@{ref} contains publish/package/deploy/pages steps: {', '.join(steps)}")
    runners = _hosted_runners(text)
    if runners and set(trig) & (_BLOCKING_ALWAYS | _BLOCKING_UNLESS_ALLOW):
        _hold(report, f"workflow {name}@{ref} runs on GitHub-hosted runners ({', '.join(runners)}) "
                      "on a trigger this work could fire")


def _inspect_workflows(gh, repo_full_name: str, ref: str,
                       allow_workflows: bool, report: TriggerReport) -> None:
    try:
        entries = gh.rest("GET", f"repos/{repo_full_name}/contents/.github/workflows"
                                 f"?ref={quote(ref, safe='')}")
    except Exception as exc:
        if _is_not_found(exc):
            report.details["workflows"][ref] = "missing"
            return
        report.details["inaccessible"].append(f".github/workflows@{ref}")
        _hold(report, f"could not list .github/workflows on {ref} ({_clip(exc)})")
        return
    if isinstance(entries, dict):
        if entries.get("message"):
            _hold(report, f"could not list .github/workflows on {ref} ({entries['message']})")
            return
        entries = [entries]
    if not isinstance(entries, list):
        _hold(report, f".github/workflows on {ref} returned an unexpected payload")
        return
    files = [e for e in entries
             if isinstance(e, dict) and e.get("type") == "file"][:MAX_WORKFLOW_FILES]
    report.details["workflows"][ref] = [str(e.get("name") or e.get("path")) for e in files]
    if len([e for e in entries if isinstance(e, dict)]) > MAX_WORKFLOW_FILES:
        _hold(report, f"more than {MAX_WORKFLOW_FILES} workflow files on {ref}; "
                      f"only the first {MAX_WORKFLOW_FILES} were inspected")
    for e in files:
        name = str(e.get("name") or e.get("path") or "workflow")
        path = str(e.get("path") or name)
        size = e.get("size") or 0
        if isinstance(size, int) and size > MAX_WORKFLOW_BYTES:
            _hold(report, f"workflow {name}@{ref} too large to inspect ({size} bytes); review manually")
            continue
        try:
            blob = gh.rest("GET", f"repos/{repo_full_name}/contents/{quote(path, safe='')}"
                                  f"?ref={quote(ref, safe='')}")
        except Exception as exc:
            _hold(report, f"could not read workflow {name}@{ref} ({_clip(exc)})")
            continue
        text = _decode_blob(blob)
        if text is None:
            _hold(report, f"workflow {name}@{ref} content unreadable; review manually")
            continue
        _flag_workflow_text(name, ref, text, allow_workflows, report)


def _inspect_rulesets(gh, repo_full_name: str, report: TriggerReport,
                      allow_unknown_rulesets: bool = False) -> None:
    try:
        rulesets = gh.rest("GET", f"repos/{repo_full_name}/rulesets")
    except Exception as exc:
        if _is_not_found(exc):
            report.details["rulesets"] = "none"
            return
        report.details["inaccessible"].append("rulesets")
        if allow_unknown_rulesets:
            report.details["rulesets"] = (
                f"unverified (owner allowlisted for this repo): {_clip(exc, 160)}")
            return
        # A free private repository cannot answer this API at all (GitHub
        # returns 403 "Upgrade to GitHub Pro..."). That is missing evidence,
        # not a known-dangerous ruleset, so record it as unverified instead
        # of holding every write on the account.
        if "upgrade to github pro" in str(exc).lower():
            try:
                info = gh.rest("GET", f"repos/{repo_full_name}")
            except Exception:
                info = {}
            if isinstance(info, dict) and info.get("private") is True:
                report.details["rulesets"] = (
                    "unverified (private repository on a plan without ruleset access)")
                return
        _hold(report, f"could not inspect repo rulesets ({_clip(exc)})")
        return
    text = json.dumps(rulesets, default=str).lower()
    if "copilot" in text:
        _hold(report, "copilot configuration found in repo rulesets")
    report.details["rulesets"] = len(rulesets) if isinstance(rulesets, list) else "present"


def _inspect_gitattributes(gh, repo_full_name: str, ref: str | None,
                           report: TriggerReport) -> None:
    if not ref:
        report.details["inaccessible"].append(".gitattributes")
        _hold(report, "no branch reference available to inspect .gitattributes for LFS")
        return
    try:
        blob = gh.rest("GET", f"repos/{repo_full_name}/contents/.gitattributes"
                              f"?ref={quote(ref, safe='')}")
    except Exception as exc:
        if _is_not_found(exc):
            report.details["gitattributes"] = "missing"
            return
        report.details["inaccessible"].append(".gitattributes")
        _hold(report, f"could not read .gitattributes on {ref} ({_clip(exc)})")
        return
    text = _decode_blob(blob) or ""
    if _has_lfs_filter(text):
        report.details["lfs"] = True
        _hold(report, f"lfs: .gitattributes on {ref} has Git LFS filters — "
                      "LFS downloads may charge the repo owner")


def preflight(gh, repo_full_name: str, head_ref: str | None = None,
              allow_workflows: bool = False,
              allow_unknown_rulesets: bool = False) -> TriggerReport:
    """Inspect everything our writes could billably start; never guess safe.

    Checks the default branch AND head_ref when given (a PR can add
    workflows), .gitattributes for LFS, repo rulesets and settings payloads
    for Copilot review config. Inaccessible areas become NEEDS_REVIEW, except
    rulesets when allow_unknown_rulesets is set (owner's per-repo choice for
    plan-limited private repos — recorded as unverified, not silently safe).
    """
    report = TriggerReport(details={
        "repo": repo_full_name, "workflows": {}, "inaccessible": [],
        "allow_workflows": bool(allow_workflows),
    })
    default_branch = None
    try:
        payload = gh.rest("GET", f"repos/{repo_full_name}")
        if isinstance(payload, dict):
            default_branch = payload.get("default_branch") or None
            if "copilot" in json.dumps(payload, default=str).lower():
                _hold(report, "copilot setting found in repository settings payload")
    except Exception as exc:
        _hold(report, f"repository metadata unreadable ({_clip(exc)}); cannot verify settings")
    refs: list[str] = []
    if default_branch:
        refs.append(str(default_branch))
    if head_ref and head_ref not in refs:
        refs.append(str(head_ref))
    report.details["refs_inspected"] = refs
    for ref in refs:
        _inspect_workflows(gh, repo_full_name, ref, allow_workflows, report)
    _inspect_rulesets(gh, repo_full_name, report,
                      allow_unknown_rulesets=allow_unknown_rulesets)
    _inspect_gitattributes(gh, repo_full_name, head_ref or default_branch, report)
    report.verdict = REVIEW if report.reasons else SAFE
    return report


def verdict_line(report: TriggerReport) -> str:
    """One plain-English line for job notes and logs."""
    if report.verdict == SAFE:
        return ("Trigger preflight: SAFE_TO_WRITE — nothing found that this "
                "worker's push, comment, or PR writes could billably start.")
    joined = "; ".join(report.reasons) if report.reasons else "unverified areas remain"
    return f"Trigger preflight: NEEDS_REVIEW — writes held: {joined}"
