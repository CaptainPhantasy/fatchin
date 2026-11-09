"""Plain-language next steps for saved fleet work; this module never changes work."""
from __future__ import annotations

from datetime import datetime, timezone
import re

from .github_client import redact_text


def safe_evidence(value):
    """Expose saved publication evidence without credential values or leases."""
    if isinstance(value, str):
        text = redact_text(value)
        text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                      '[redacted private key]', text, flags=re.S)
        return re.sub(r'(?i)(\b(?:api[_-]?key|access[_-]?token|secret|password|authorization)\s*[:=]\s*)[^\s,;]+',
                      r'\1[redacted]', text)
    if isinstance(value, list):
        return [safe_evidence(entry) for entry in value]
    if isinstance(value, dict):
        return {key: ('[redacted]' if re.search(r'(?i)(?:token|secret|password|authorization|credential|api[_-]?key)|^(?:lease|process|pid)$', str(key))
                      else safe_evidence(entry)) for key, entry in value.items()}
    return value


def failed_checks(item):
    checks = (item.get('result') or {}).get('checks') or item.get('checks') or []
    for check in checks:
        if not isinstance(check, dict):
            continue
        exit_code = check.get('exit', check.get('exit_code'))
        if check.get('passed') is False or check.get('status') == 'failed':
            return True
        if exit_code is not None:
            try:
                if int(exit_code) != 0:
                    return True
            except (TypeError, ValueError):
                return True
    return False


def fleet_recovery(item, fleet, data=None):
    """Describe an actual available route, keeping publication decisions with people."""
    result = item.get('result') or {}
    detail = str(item.get('error') or result.get('error') or item.get('blocked_reason') or result.get('summary') or '')
    lowered = detail.lower()
    status = item.get('status')
    running = fleet.get('status') == 'running'
    repo = str(item.get('repo') or '').casefold()
    excluded = bool(item.get('scope_excluded')) or (
        fleet.get('scope') == 'selected' and repo not in {str(r).casefold() for r in fleet.get('repos', [])})
    if 'repositories' in fleet and repo not in {str(r.get('repo', '')).casefold() for r in fleet['repositories']}:
        excluded = True
    can_retry = status in {'blocked', 'failed', 'dismissed'} or (status == 'done' and failed_checks(item))
    response = dict(kind='finished' if status in {'done', 'dismissed'} else 'waiting',
                    title='Work finished' if status in {'done', 'dismissed'} else 'Waiting for its turn',
                    explanation=detail or ('This result is saved. You can read its evidence or move it to history.' if status in {'done', 'dismissed'} else 'This work is saved and has not started a new action.'),
                    next_step='Open the saved result to read what happened.', can_retry=can_retry and running,
                    needs_checks=False, actions=['open_details'])
    def choose(kind, title, explanation, next_step, actions, *, retry=False, checks=False):
        response.update(kind=kind, title=title, explanation=explanation, next_step=next_step,
                        can_retry=retry and running, needs_checks=checks, actions=actions)
        return response
    if item.get('archived_at'):
        return choose('archived', 'Saved in history', 'You moved this item out of the working list. Its result, errors and published-change records are kept.',
                      'Read the saved evidence, or restore the item when you want to work on it again.', ['open_details', 'restore'])
    if excluded or 'outside the current writable scope' in lowered:
        return choose('scope', 'This project is outside your fleet’s selection', detail or 'This fleet does not have permission to work in this project.',
                      'Review the fleet’s project selection and GitHub access. You can also archive this old item without changing GitHub.',
                      ['review_scope', 'check_access', 'open_details', 'archive'])
    if any(text in lowered for text in ('no longer open', 'pull request closed', 'pull request is closed', 'newer commit replaced')):
        return choose('obsolete', 'This work no longer applies', detail,
                      'Open the pull request to confirm its current state, then archive this item. Start new work for a different open request.',
                      ['open_github', 'open_details', 'archive'])
    if any(text in lowered for text in ('manual review', 'complete diff cannot', 'complete diff could not', 'cannot inspect the complete')):
        return choose('manual_review', 'A person needs to review this change', detail,
                      'Open the change on GitHub and inspect the full files and discussion. Archive this item after you have handled the review; archiving does not claim the code passed.',
                      ['open_github', 'open_details', 'archive'])
    if any(text in lowered for text in ('beyond its assigned files', 'too broad', 'narrower request')):
        return choose('narrow_request', 'The request needs a smaller scope', detail,
                      'Read the original finding on GitHub and make it specific about the files and result you want. Then retry, or archive the old finding.',
                      ['open_github', 'open_details', 'retry' if running else 'resume', 'archive'], retry=can_retry)
    budget = fleet.get('max_calls_total') or 0
    used = fleet.get('calls_used') or 0
    if budget and used >= budget:
        return choose('budget', 'The authorized model-call budget has been used',
                      f'This fleet has used {used} of {budget} authorized model calls. No further work will start until you decide.',
                      'Authorize a specific number of additional calls to continue, or leave the fleet paused. Existing results remain available.',
                      ['authorize_budget', 'open_details', 'archive'] if can_retry else ['authorize_budget', 'open_details'])
    if status == 'retrying':
        until = item.get('eligible_at')
        provider = item.get('provider') or 'the selected provider'
        limits = (data or {}).get('rate_limits') or {}
        waiting = [(key, val) for key, val in limits.items() if val.get('until', '') > datetime.now(timezone.utc).isoformat(timespec='seconds')]
        response.update(retry_at=max([until or ''] + [val.get('until', '') for _, val in waiting]),
                        waiting_for=', '.join(key for key, _ in waiting) or provider)
        return choose('cooldown', 'Waiting before the next automatic attempt', detail or 'The provider or GitHub asked this fleet to wait.',
                      'The app shows the next attempt time. You can wait; no action is required. Open connection checks if failures continue.',
                      ['open_details', 'diagnostics'])
    if failed_checks(item) or result.get('next_action') == 'configure_checks' or item.get('next_action') == 'configure_checks':
        return choose('checks', 'The project’s check needs attention', detail or 'The saved test or build command did not pass, or no suitable command was found.',
                      'Read the failed command and output. Set the project’s real test or build command if needed, then retry after correcting the reported problem.' + ('' if running else ' Resume this fleet before retrying.'),
                      ['configure_checks', 'open_details', 'retry' if running else 'resume', 'archive'], retry=can_retry, checks=True)
    if any(text in lowered for text in ('provider', 'http 403', 'http 401', 'api key', 'credential', 'unauthorized', 'connection')):
        return choose('provider', 'The service connection needs attention', detail,
                      'Open connection checks to identify the affected provider. Repair or test that connection, then retry this item.' + ('' if running else ' Resume this fleet when you are ready.'),
                      ['diagnostics', 'connections', 'open_details', 'retry' if running else 'resume', 'archive'], retry=can_retry)
    operations = item.get('mutation_intents') or item.get('operations') or []
    uncertain = any(isinstance(op, dict) and op.get('state') in {'uncertain', 'sending', 'unknown'} for op in operations)
    if uncertain or (status in {'blocked', 'failed'} and any(isinstance(op, dict) and op.get('state') == 'prepared' for op in operations)) or item.get('publish_outcome') == 'uncertain' or 'uncertain' in lowered or 'could not be confirmed' in lowered:
        return choose('publication_uncertain', 'A published change needs to be checked', detail or 'I don’t know whether GitHub accepted the previous change.',
                      'Open the saved publication evidence and GitHub before deciding. A retry first reconciles saved write records; archiving keeps those records and sends nothing.',
                      ['open_details', 'open_github', 'retry' if running else 'resume', 'archive'], retry=can_retry)
    if can_retry and not running:
        return choose('fleet_stopped', 'This work is saved; its fleet is not running', detail or 'No new attempt will run while this fleet is stopped or paused.',
                      'Resume the fleet when you want it to work again, then retry this item. You can archive the item if you no longer need it.',
                      ['resume', 'open_details', 'archive'])
    if can_retry:
        return choose('failed', 'This item needs your attention', detail or 'The last attempt did not finish successfully.',
                      'Read the result and check output, correct the named problem, then retry. You can archive this item while keeping all its evidence.',
                      ['open_details', 'diagnostics', 'retry', 'archive'], retry=True)
    if status in {'queued', 'retrying'} and not running:
        return choose('fleet_stopped', 'Queued; the fleet is stopped or paused', 'This work is saved. It will not run until its fleet resumes.',
                      'Choose Resume fleet to allow new work, or leave it stopped until you are ready.', ['resume', 'open_details'])
    if status == 'running':
        return choose('running', 'An agent is working on this item', item.get('stage') or 'The current step is still in progress.',
                      'You can read its progress, pause the fleet after current work, or stop the fleet to request a safe stop.', ['open_details', 'pause', 'stop'])
    readiness = item.get('readiness') or result.get('readiness') or {}
    if status == 'done' and item.get('kind') == 'review_pr' and not readiness.get('ready'):
        return choose('merge_waiting', 'The review finished; merging still needs attention',
                      str(readiness.get('reason') or 'I don’t know whether this change is ready to merge. Current merge conditions have not been confirmed.'),
                      'Read the saved review and open GitHub to inspect the current checks, findings and required approvals. Merging stays your decision.',
                      ['open_details', 'open_github', 'archive'])
    if status in {'done', 'dismissed'}:
        response['actions'].append('archive')
    return response
