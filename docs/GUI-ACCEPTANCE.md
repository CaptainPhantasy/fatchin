# Testing and acceptance

The public evidence for this project is its offline test suite:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

The suite uses real Git repositories, real worktrees, real file locks, real
loopback HTTP, and bubblewrap when available. GitHub and model endpoints are
simulated. No test calls a live provider.

What that means:

- A passing check in a test proves the code path, not that a particular model
  produces good review comments.
- Dashboard and browser checks are not part of the default suite. The interface
  is vanilla JavaScript; run it locally to inspect it.
- No dated live-run receipts are published in this repository. If you need
  evidence for a deployment, keep your own.

Boundaries that are tested directly:

- no merge call exists; no force-push is issued;
- a draft pull request is created before review;
- a correction is reviewed by a different slot;
- dry run records intended writes and performs none;
- repository policy keeps organisation namespaces out;
- checks run without network or account files;
- a free private repository's ruleset 403 is recorded as unverified rather than
  as safe.
