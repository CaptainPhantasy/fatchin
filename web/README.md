# Browser assets

The workspace is a dependency-free browser application. `index.html`, `app.js`,
`styles.css`, and the SVG assets are served by the worker's Python web service;
there is no package manager, bundler, external CDN, or browser tracking code.
The JSON API is always requested from the same origin.

The interface uses semantic HTML, native dialog focus management, visible
keyboard focus, reduced-motion support, explicit labels, and a responsive
navigation drawer. All text returned by the server is escaped before insertion.
GitHub descriptions and comments are displayed as text rather than executable
HTML. No model request is made by loading the interface or running setup checks.

Original SVG line icons and the application mark were created for this interface.
The self-hosted Inter regular and semibold fonts are distributed under the
SIL Open Font License 1.1. Their full upstream notice is preserved in
[`fonts/Inter-LICENSE.txt`](fonts/Inter-LICENSE.txt). No license is applied to the
application by this font notice.

Operational and user documentation is in `docs/GUI-GUIDE.md`,
`docs/GUI-OPERATIONS.md`, `docs/GUI-CAPABILITIES.md`, and
`docs/GUI-ACCEPTANCE.md` in the project root.

For a basic syntax check, run:

```sh
node --check web/app.js
```

Functional checks should use the real HTTP service with isolated worker records,
and should not queue a task or send a provider test request unless that action is
intended. The final task review and watch acknowledgment explain model usage and
GitHub writes before submission.

Two browser checks use the host's existing Google Chrome and Node.js 22+ without
installing any browser package:

```sh
node tests/gui-browser.mjs http://YOUR-MESH-ADDRESS:18465 /home/YOU/gui-live-receipts
node tests/gui-browser-fixture.mjs http://YOUR-MESH-ADDRESS:18465 /home/YOU/gui-fixture-receipts
```

The first checks the installed application with read-only GitHub and filesystem
requests. It never queues a task, changes saved settings, connects an account,
stores a credential, creates a folder, or makes a provider request. It selects an
existing writable subfolder belonging to the current operating-system account;
if no open issue or matching account exists, the task-review portion is reported
as skipped.

The second intercepts every `/api/*` request inside an isolated browser and
fulfills it with test data. Its submission checks cannot reach the live API. It
tests confirmations, explicit output folders, queued-task controls, stale saves,
error recovery, credential handling, source-text escaping, and individual file
download links. Its provider call counter is entirely simulated. HTTP 400, 409,
and 503 browser messages are deliberately exercised failure cases.

Both runners create a disposable browser profile, close their own browser, and
remove that profile afterward. Screenshots and JSON receipts remain in the
explicit destination folder. The Chrome executable defaults to
`/usr/bin/google-chrome`; adjust that path in the runner if using another
installation.
