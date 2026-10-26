"""Read-only account-wide discovery. Failed reads never imply closure or readiness."""
from __future__ import annotations

import re
from urllib.parse import quote

from .fleet_store import identity, now
from .github_client import coerce_github_id, RateLimited

_NAME = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
_MARKER = re.compile(r'<!--\s*(?:fatchin|gha-worker)[^>]*-->')
_SENSITIVE = re.compile(r'(^|/)(?:\.env(?:\..*)?|\.git|\.ssh|credentials?[^/]*|secrets?[^/]*|id_rsa|id_ed25519)(?:/|$)|\.(?:pem|key|p12|pfx)$', re.I)
_CODE = {'.py', '.js', '.ts', '.jsx', '.tsx', '.mjs', '.go', '.rs', '.java', '.rb', '.php', '.c', '.cc', '.cpp', '.h', '.sh', '.sql', '.swift', '.kt'}

_READINESS_QUERY = '''query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){headRefOid isDraft state mergeable mergeStateStatus reviewDecision commits(last:1){nodes{commit{statusCheckRollup{state}}}} reviewThreads(first:100,after:$cursor){pageInfo{hasNextPage endCursor} nodes{id isResolved isOutdated comments(first:100){nodes{databaseId} pageInfo{hasNextPage}}}}}}}'''


class DiscoveryError(ValueError):
    pass


def _policy_allows(full_name, policy, account):
    """Personal repos default on; organisation repos default off.

    Include/exclude always wins over the namespace default. An unset policy
    keeps the previous behaviour (everything writable is in scope).
    """
    include = {str(x).casefold() for x in (policy.get('include') or [])}
    exclude = {str(x).casefold() for x in (policy.get('exclude') or [])}
    name = str(full_name).casefold()
    if name in include:
        return True
    if name in exclude:
        return False
    owner = name.split('/', 1)[0]
    key = 'personal_default' if owner == str(account).casefold() else 'org_default'
    default = policy.get(key)
    if default not in ('include', 'exclude'):
        default = 'include'
    return default == 'include'


def _apply_policy(repositories, policy, account):
    kept, skipped = [], []
    for repository in repositories:
        if _policy_allows(repository.get('full_name', ''), policy, account):
            kept.append(repository)
        else:
            skipped.append(repository.get('full_name', ''))
    return kept, skipped


class FleetDiscovery:
    def __init__(self, gh, cfg, store):
        self.gh, self.cfg, self.store = gh, cfg, store
        self._permissions = {}

    def _get(self, path, *, paginate=False):
        if self.store.internal().get('rate_limits', {}).get('github', {}).get('until', '') > now():
            raise DiscoveryError('GitHub asked this fleet to wait before retrying.')
        try:
            result = self.gh.rest('GET', path, paginate=paginate)
        except RateLimited as exc:
            self.store.rate_limit('github', exc.retry_after_seconds or 900)
            raise
        if paginate and not isinstance(result, list):
            raise DiscoveryError('GitHub returned an incomplete list.')
        return result

    def _trusted(self, repo, comment):
        login = (comment.get('user') or {}).get('login', '')
        if not re.fullmatch(r'[A-Za-z0-9-]+(?:\[bot\])?', login):
            return False
        key = (repo, login)
        if key not in self._permissions:
            value = self._get(f'/repos/{repo}/collaborators/{quote(login, safe="")}/permission')
            self._permissions[key] = value.get('permission') in {'admin', 'maintain', 'write'}
        return self._permissions[key]

    def _readiness(self, repo, number, expected_head, reviewed=None):
        owner, name = repo.split('/')
        cursor, seen, resolved, thread_ids = None, set(), set(), {}
        all_resolved = True
        evidence = None
        while True:
            try:
                response = self.gh.graphql(_READINESS_QUERY, {'owner': owner, 'name': name, 'number': number, 'cursor': cursor})
            except RateLimited as exc:
                self.store.rate_limit('github', exc.retry_after_seconds or 900)
                raise
            node = ((response or {}).get('repository') or {}).get('pullRequest')
            if not isinstance(node, dict):
                raise DiscoveryError('GitHub could not provide current merge requirements.')
            threads = node.get('reviewThreads')
            if not isinstance(threads, dict) or not isinstance(threads.get('nodes'), list):
                raise DiscoveryError('GitHub review threads could not be read completely.')
            for thread in threads['nodes']:
                if not thread.get('isResolved'):
                    all_resolved = False
                comments = thread.get('comments') or {}
                if comments.get('pageInfo', {}).get('hasNextPage'):
                    raise DiscoveryError('A review thread is too large to verify completely.')
                for comment in comments.get('nodes', []):
                    if thread.get('id'):
                        thread_ids[str(comment.get('databaseId'))] = thread['id']
                if thread.get('isResolved'):
                    resolved.update(str(c.get('databaseId')) for c in comments.get('nodes', []))
            nodes = ((node.get('commits') or {}).get('nodes') or [])
            rollup = ((nodes[-1].get('commit') or {}).get('statusCheckRollup') if nodes else None)
            checks_success = isinstance(rollup, dict) and rollup.get('state') == 'SUCCESS'
            checks_source = 'GitHub'
            result = (reviewed or {}).get('result') or {}
            local_checks = result.get('checks') or []
            if (rollup is None and result.get('verified_head_sha') == expected_head
                    and local_checks and all(c.get('passed') is True for c in local_checks)):
                checks_success = True
                checks_source = 'Local checks on the reviewed commit'
            # CLEAN is GitHub's aggregate gate, so absent/no-required-review is not invented approval.
            clean = node.get('mergeStateStatus') == 'CLEAN'
            review = node.get('reviewDecision')
            ready = (node.get('headRefOid') == expected_head and node.get('state') == 'OPEN'
                     and node.get('isDraft') is False and node.get('mergeable') == 'MERGEABLE'
                     and clean and review in (None, '', 'APPROVED') and checks_success)
            evidence = {'ready': ready, 'head_sha': node.get('headRefOid'), 'checked_at': now(),
                        'mergeable': node.get('mergeable'), 'merge_state': node.get('mergeStateStatus'),
                        'review_decision': review, 'checks_success': checks_success, 'checks_source': checks_source,
                        'reason': '' if ready else 'Waiting for current checks, approvals, a fresh review, or merge requirements.'}
            page = threads.get('pageInfo')
            if not isinstance(page, dict):
                raise DiscoveryError('Review thread pagination is missing.')
            if not page.get('hasNextPage'):
                break
            cursor = page.get('endCursor')
            if not cursor or cursor in seen:
                raise DiscoveryError('Review thread pagination did not advance.')
            seen.add(cursor)
        evidence['ready'] = evidence['ready'] and all_resolved
        if not all_resolved:
            evidence['reason'] = 'Open review threads need attention.'
        return evidence, resolved, thread_ids

    @staticmethod
    def _correction(internal, repo, number, actual_pr=None):
        matches = []
        for item in internal['items'].values():
            value = item.get('fix_pr') or (item.get('result') or {}).get('fix_pr')
            if value and value.get('repo') == repo and value.get('number') == number:
                matches.append((int(value.get('repair_round') or 0), item.get('updated_at', ''), value))
            # A remote PR can appear before its publisher saves finish(). Local intent plus
            # exact remote head/ref/body proves provenance during that crash window.
            if actual_pr:
                for operation in item.get('mutation_intents', []):
                    payload = operation.get('payload') or {}
                    head = actual_pr.get('head') or {}
                    base = actual_pr.get('base') or {}
                    previous = payload.get('correction_pr')
                    if (operation.get('kind') != 'push' or payload.get('repo') != repo
                            or payload.get('prepared_commit_sha') != head.get('sha')
                            or payload.get('branch') != head.get('ref')
                            or payload.get('target_ref') != base.get('ref')):
                        continue
                    if previous:
                        if previous.get('repo') != repo or previous.get('number') != number:
                            continue
                    elif not payload.get('pr_body') or actual_pr.get('body') != payload['pr_body']:
                        continue
                    inferred = {**(previous or {}), 'repo': repo, 'repo_id': payload.get('repository_id'),
                        'number': number, 'url': actual_pr.get('html_url'), 'branch': payload['branch'],
                        'head_sha': head['sha'], 'target_ref': payload['target_ref'],
                        'source_repo': item.get('source_repo'), 'source_ref': item.get('source_ref'),
                        'root_number': (previous or {}).get('root_number', item.get('number')),
                        'root_finding_id': payload.get('root_finding_id'),
                        'repair_round': int(previous.get('repair_round') or 0) + 1 if previous else 0,
                        'author_slot': item.get('slot'), 'publication_pending': True}
                    matches.append((inferred['repair_round'], item.get('updated_at', ''), inferred))
        # Repairs reuse one correction PR. The highest confirmed round owns its head and author.
        return max(matches, key=lambda row: (row[0], row[1], not row[2].get('publication_pending', False)))[2] if matches else None

    def _pr(self, repo, repository, listed, internal):
        number = listed.get('number')
        if type(number) is not int or number < 1:
            raise DiscoveryError('GitHub returned an invalid pull request number.')
        pr = self._get(f'/repos/{repo}/pulls/{number}')
        head, base = pr.get('head') or {}, pr.get('base') or {}
        source = head.get('repo') or {}
        common = {'repo': repo, 'repo_id': repository['id'], 'number': number,
                  'title': str(pr.get('title') or f'Pull request #{number}'), 'url': pr.get('html_url', ''),
                  'created_at': pr.get('created_at') or now(), 'head_sha': head.get('sha', ''),
                  'base_sha': base.get('sha', ''), 'source_repo': source.get('full_name'),
                  'source_repo_id': source.get('id'), 'source_ref': head.get('ref'),
                  'target_repo': source.get('full_name'), 'target_repo_id': source.get('id'),
                  'target_ref': head.get('ref'), 'base_ref': base.get('ref'), 'discovered_at': now()}
        correction = self._correction(internal, repo, number, pr)
        if correction:
            common.update(correction_pr=correction, author_slot=correction.get('author_slot'))
        reviewed = next((i for i in internal['items'].values() if i['kind'] == 'review_pr'
                         and i['repo'] == repo and i['number'] == number and i.get('head_sha') == common['head_sha']
                         and i['status'] == 'done'), None)
        evidence, resolved, thread_ids = self._readiness(repo, number, common['head_sha'], reviewed)
        candidates = [{**common, 'kind': 'review_pr', 'readiness': evidence}]
        known_comment_ids = {str(i.get('posted_comment_id') or i.get('comment_id'))
                             for i in internal['items'].values() if i.get('trusted_provenance') and i.get('repo') == repo}
        sources = [('conversation', f'/repos/{repo}/issues/{number}/comments?per_page=100'),
                   ('inline', f'/repos/{repo}/pulls/{number}/comments?per_page=100'),
                   ('review', f'/repos/{repo}/pulls/{number}/reviews?per_page=100')]
        for kind, endpoint in sources:
            for comment in self._get(endpoint, paginate=True):
                body = str(comment.get('body') or '').strip()
                if not body or str(comment.get('id')) in known_comment_ids:
                    continue
                if kind == 'inline' and str(comment.get('id')) in resolved:
                    continue
                # Fleet narration is never new fix work. Generated findings are admitted locally by finish().
                if _MARKER.search(body):
                    continue
                if not self._trusted(repo, comment):
                    continue
                comment_id = f"{kind}:{comment.get('id')}"
                candidates.append({**common, 'kind': 'fix_comment', 'title': body.splitlines()[0][:160],
                    'body': body, 'path': comment.get('path', ''), 'line': comment.get('line') or comment.get('original_line'),
                    'url': comment.get('html_url') or common['url'], 'source_url': comment.get('html_url') or common['url'],
                    'comment_id': comment_id, 'comment_revision': comment.get('updated_at') or identity(body),
                    'created_at': comment.get('created_at') or common['created_at'],
                    'comment_source': kind, 'thread_id': thread_ids.get(str(comment.get('id'))) if kind == 'inline' else None, 'comment_author': (comment.get('user') or {}).get('login'),
                    'trusted_collaborator': True})
        closed = [i['id'] for i in internal['items'].values() if i['kind'] == 'fix_comment'
                  and i['repo'] == repo and i.get('number') == number
                  and str(i.get('comment_id', '')).removeprefix('inline:') in resolved]
        return candidates, evidence, closed

    MAX_COMMIT_FILES = 25

    def _code(self, repo, repository, branch=None):
        """One review per changed-file batch of a commit, never per file.

        Per-file review made a 300-repository account produce tens of
        thousands of near-identical calls and could not see a change that
        spans files. The unit is now the pushed commit, compared with its
        first parent, in bounded batches.
        """
        branch = branch or repository.get('default_branch')
        if not branch:
            return [], ''
        commit = self._get(f'/repos/{repo}/commits/{quote(branch, safe="")}')
        head = commit.get('sha')
        parents = commit.get('parents') or []
        base = (parents[0] or {}).get('sha') if parents else ''
        if not head or not base:
            # A root commit has no parent to compare against; it is left
            # unreviewed rather than guessed at from a different baseline.
            return [], head
        stats = commit.get('stats') or {}
        changed = [f for f in (commit.get('files') or [])
                   if isinstance(f, dict) and isinstance(f.get('filename'), str)]
        total = stats.get('total') if type(stats.get('total')) is int else len(changed)
        code = []
        for entry in changed:
            path = entry['filename']
            extension = '.' + path.rsplit('.', 1)[-1] if '.' in path else ''
            if (extension.lower() not in _CODE or _SENSITIVE.search(path)
                    or entry.get('changes', 0) > 2000
                    or any(p in path.split('/') for p in ('node_modules', 'vendor', 'dist', 'build'))):
                continue
            code.append(path)
        code = sorted(dict.fromkeys(code))
        batches = [code[i:i + self.MAX_COMMIT_FILES]
                   for i in range(0, len(code), self.MAX_COMMIT_FILES)]
        truncated = total > len(changed) or len(batches) > 12
        committed = ((commit.get('commit') or {}).get('committer') or {}).get('date') or now()
        result = []
        for index, paths in enumerate(batches[:12]):
            if not paths:
                continue
            result.append({
                'kind': 'review_code', 'repo': repo, 'repo_id': repository['id'], 'number': 0,
                'title': f'Review {len(paths)} changed file(s) in {repo} at {head[:7]}',
                'paths': paths, 'path': paths[0], 'batch': index,
                'url': f'https://{self.cfg.github_host}/{repo}/commit/{head}',
                'head_sha': head, 'base_sha': base, 'truncated': truncated,
                'commit_subject': ((commit.get('commit') or {}).get('message') or '').splitlines()[0][:160],
                'source_repo': repo, 'source_repo_id': repository['id'], 'source_ref': branch,
                'target_repo': repo, 'target_repo_id': repository['id'], 'target_ref': branch,
                'dedup_key': f"code:{repository['id']}:{head}:{index}:{identity(paths)}",
                'created_at': committed,
            })
        return result, head

    def scan(self):
        internal = self.store.internal()
        fleets = [f for f in internal['fleets'].values() if f['status'] in {'running', 'paused'}]
        if not fleets:
            return
        self._permissions = {}
        for fleet in fleets:
            self.store.upsert(fleet['id'], [], error='')
        try:
            user = self._get('/user')
            github_id = coerce_github_id(user.get('id'))
            repositories = self._get('/user/repos?affiliation=owner,collaborator,organization_member&per_page=100&sort=updated', paginate=True)
            repositories = [r for r in repositories if isinstance(r, dict) and _NAME.fullmatch(r.get('full_name', ''))
                            and type(r.get('id')) is int and not r.get('archived') and not r.get('disabled')
                            and any((r.get('permissions') or {}).get(p) is True for p in ('push', 'maintain', 'admin'))]
        except Exception:
            for fleet in fleets:
                self.store.upsert(fleet['id'], [], error='The connected GitHub account or repository list could not be read. Discovery will retry automatically.')
            return
        for fleet in fleets:
            if github_id != fleet['github_id']:
                self.store.upsert(fleet['id'], [], error='The connected GitHub identity changed. Reconnect the original account before continuing.')
                continue
            selected = [r for r in repositories if fleet.get('scope') != 'selected' or r['full_name'] in fleet.get('repos', [])]
            selected, policy_skipped = _apply_policy(
                selected, getattr(self.cfg, 'fleet_policy', {}) or {},
                str(fleet.get('github_login') or fleet.get('account') or ''))
            readiness, failed, resolved, finalizations = {}, set(), [], {}
            branches, conditions, open_pr_heads = {}, {}, {}
            promoted = set()
            descriptors = [{'repo': r['full_name'], 'id': r['id'], 'private': bool(r.get('private'))} for r in selected]
            # Apply removed access and scope before any slower per-repository network reads.
            self.store.upsert(fleet['id'], [], repositories=descriptors, scanned_repos=[],
                              policy_skipped=policy_skipped)
            listed = {}
            work = []
            # Fetch the headers first so the first published PR is newest across the whole account.
            for repository in selected:
                repo = repository['full_name']
                try:
                    prs = self._get(f'/repos/{repo}/pulls?state=open&sort=created&direction=desc&per_page=100', paginate=True)
                    listed[repo] = prs
                    work.extend((repository, pr) for pr in prs)
                except Exception:
                    failed.add(repo)
            work.sort(key=lambda pair: pair[1].get('created_at', ''), reverse=True)
            for repository, pr in work:
                repo = repository['full_name']
                try:
                    candidates, evidence, closed = self._pr(repo, repository, pr, internal)
                    readiness[f"{repo}#{pr['number']}"] = evidence
                    resolved.extend(closed)
                    # Complete per-PR batches are runnable immediately; later repository reads do not delay them.
                    self.store.upsert(fleet['id'], candidates, scanned_repos=[repo], readiness=readiness,
                                      resolved_items=closed, repositories=descriptors)
                except Exception:
                    failed.add(repo)
                    readiness.pop(f"{repo}#{pr['number']}", None)
                    self.store.upsert(fleet['id'], [], scanned_repos=[repo], readiness=readiness, failed_repos=[repo])
            # Code snapshots and merged-correction finalization follow the foreground PR pass.
            cached = dict(fleet.get('code_scan_heads') or {})
            for repository in selected:
                repo = repository['full_name']
                candidates = []
                try:
                    for branch in self._get(f'/repos/{repo}/branches?per_page=100', paginate=True):
                        name = branch.get('name')
                        sha = (branch.get('commit') or {}).get('sha')
                        if not name or not sha:
                            raise DiscoveryError('A branch could not be read completely.')
                        key = repo + ':' + name
                        branches[key] = sha
                        own_pr = any(p.get('head', {}).get('ref') == name and (p.get('head', {}).get('repo') or {}).get('full_name') == repo
                                     for p in listed.get(repo, []))
                        default_branch = repository.get('default_branch')
                        meta = (branch.get('commit') or {}).get('commit') or {}
                        known = cached.get(key)
                        if not own_pr and name != default_branch:
                            if known is None:
                                # First sighting is the baseline, not a push.
                                # Only a later head change is promoted, so an
                                # existing account does not open a PR for every
                                # old branch on the first scan.
                                cached[key] = sha
                                self.store.upsert(fleet['id'], [], scanned_repos=[], branch_heads={key: sha})
                            elif known != sha:
                                # A pushed feature branch becomes a draft pull
                                # request first, so the review happens on the
                                # change people will actually merge.
                                candidate = {
                                    'kind': 'open_pr', 'repo': repo, 'repo_id': repository['id'], 'number': 0,
                                    'title': 'Open a pull request for ' + name,
                                    'source_repo': repo, 'source_repo_id': repository['id'], 'source_ref': name,
                                    'target_repo': repo, 'target_repo_id': repository['id'],
                                    'target_ref': default_branch, 'base_ref': default_branch, 'head_sha': sha,
                                    'commit_subject': (((meta.get('message') or '').strip().splitlines()
                                                        or ['Changes from ' + name])[0])[:120],
                                    'url': f'https://{self.cfg.github_host}/{repo}/tree/{quote(name)}',
                                    'dedup_key': f"open-pr:{repository['id']}:{name}:{sha}",
                                    'new_push': True,
                                    'created_at': meta.get('committer', {}).get('date') or now()}
                                candidates.append(candidate)
                                promoted.add(key)
                                cached[key] = sha
                                self.store.upsert(fleet['id'], [candidate], scanned_repos=[], branch_heads={key: sha})
                        elif not own_pr and known != sha:
                            code, pinned = self._code(repo, repository, name)
                            candidates.extend(code)
                            branches[key] = pinned
                            cached[key] = pinned
                            # Publish each complete branch, so a large account can start before the scan ends.
                            self.store.upsert(fleet['id'], code, scanned_repos=[], branch_heads={key: pinned})
                    settings = next((r for r in self.cfg.repos if r.full_name == repo), None)
                    conditions[repo] = identity({'permissions': repository.get('permissions'),
                                                'checks': getattr(settings, 'checks', None),
                                                'allow_unknown_rulesets': getattr(settings, 'allow_unknown_rulesets', False)})
                    for item in internal['items'].values():
                        correction = item.get('fix_pr') or (item.get('result') or {}).get('fix_pr')
                        if item['fleet_id'] != fleet['id'] or item['kind'] != 'fix_comment' or not correction or item.get('resolved_at'):
                            continue
                        if correction.get('repo') != repo:
                            continue
                        actual = self._get(f"/repos/{repo}/pulls/{int(correction['number'])}")
                        if actual.get('merged') is True and actual.get('merged_at'):
                            # Every finding gets the same destination/ancestry
                            # verification, including conversation-only comments.
                            finalizations[item['id']] = {'repo': repo, 'number': correction['number'],
                                'merged_at': actual['merged_at'], 'merge_commit_sha': actual.get('merge_commit_sha')}
                    if repo not in failed and repo in listed:
                        open_pr_heads[repo] = {str(p['number']): (p.get('head') or {}).get('sha') for p in listed[repo]}
                    self.store.upsert(fleet['id'], [], scanned_repos=[], branch_heads=branches,
                                      repository_conditions=conditions, finalizations=finalizations, resolved_items=resolved,
                                      code_scan_heads=cached)
                except Exception:
                    failed.add(repo)
            # Read failures remove current readiness without interpreting missing data as closure.
            readiness = {key: value for key, value in readiness.items() if key.split('#')[0] not in failed}
            self.store.upsert(fleet['id'], [], repositories=descriptors, scanned_repos=None, readiness=readiness,
                              failed_repos=sorted(failed), resolved_items=resolved, branch_heads=branches,
                              repository_conditions=conditions, open_pr_heads=open_pr_heads, finalizations=finalizations,
                              code_scan_heads=cached, policy_skipped=policy_skipped,
                              promoted_branches=sorted(promoted))
            self.store.supervisor_status(discovery_error=('Could not completely refresh: ' + ', '.join(sorted(failed)[:20])) if failed else '',
                                         discovery_failed_repos=sorted(failed))
