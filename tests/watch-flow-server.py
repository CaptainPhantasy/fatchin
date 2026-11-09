"""Real temporary web-control files; only GitHub and process calls are fake."""
import json
import signal
import unittest

from ghagent.interfaces import JobState
from ghagent.web import make_server
from tests.test_web_control import GuiFixture


class Fixture(GuiFixture, unittest.TestCase):
    pass


fixture = Fixture()
fixture.build_gui()
control = fixture.control
bob_home = fixture.root / 'bob'
bob_home.mkdir()
bob_output = bob_home / 'manual-results'
bob_output.mkdir()
control._accounts.append({'account':'bob', 'label':'Bob', 'path':str(bob_home), 'uid':4001})
task_output = fixture.output
fleet_output = fixture.home / 'fleet-results'
fleet_output.mkdir()
other_output = fixture.home / 'other-watch-results'
other_output.mkdir()
stale_output = fixture.home / 'stale-results'
stale_output.mkdir()
# Seed independent TOML text so this fixture also catches the original serializer failure.
text = fixture.config_path.read_text()
repos_text = ''.join('\n[[repos]]\nowner="owner"\nname=' + json.dumps(name) +
                     '\nbase_branch="main"\nchecks=["python3 -m unittest"]\n'
                     for name in ('new-watch', 'browse-only', 'stale'))
text = text.replace('[model]', repos_text + '\n[model]')
fixture.config_path.write_text(text + '\n[fleet_policy]\npersonal_default="include"\norg_default="exclude"\ninclude=["owner/new-project"]\n[fleet_policy.review]\nnotes=["keep this policy"]\n')
original_policy = control.raw_config()['fleet_policy']
fixture.persisted_job(state=JobState.DONE, repo_full_name='owner/new-project',
    notes={'member':'alice', 'member_home':str(fixture.home), 'output_dir':str(task_output), 'receipt':'original-task-kept'})
fixture.persisted_job(job_id='job_stale', dedup_key='stale', state=JobState.DONE, repo_full_name='owner/stale',
    notes={'member':'alice', 'member_home':str(fixture.home), 'output_dir':str(stale_output)})
control.watches().put('owner/other', '*', {'scope':'all', 'enabled':False, 'member':'alice',
    'member_home':str(fixture.home), 'output_dir':str(other_output), 'last_review_comment_id':91,
    'watch_history':[{'receipt':'original-watch-history-kept'}]})
control.watches().put('owner/stale', '*', {'scope':'all', 'enabled':False, 'setup_required':True,
    'last_review_comment_id':73, 'watch_history':[{'receipt':'stale-watch-history-kept'}]})
store = control.fleet_store()
with store._locked(True) as data:
    data['fleets']['fleet_saved'] = {'id':'fleet_saved', 'status':'stopped', 'account':'alice',
        'output_dir':str(fleet_output), 'member_home':str(fixture.home), 'scope':'selected',
        'repos':['owner/new-project'], 'repositories':[], 'workers':0, 'deepseek_workers':0}

original_rest = fixture.gh.rest
def github_read_only(method, *args, **kwargs):
    if method != 'GET':
        raise AssertionError('This browser audit must not write to GitHub')
    return original_rest(method, *args, **kwargs)
fixture.gh.rest = github_read_only
server = make_server(control, port=0)
def stop(signum, frame):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}', 'root':str(fixture.root),
    'config':str(fixture.config_path), 'watch_file':str(control.watches().path),
    'queue_file':str(control.queue().jobs_path), 'policy':original_policy,
    'task_output':str(task_output), 'fleet_output':str(fleet_output), 'other_output':str(other_output),
    'stale_output':str(stale_output), 'bob_output':str(bob_output)}), flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
    fixture.doCleanups()
