# Contributing

Thanks for helping. FatChin is deliberately small: standard-library Python, a
vanilla-JS interface, and no runtime dependencies.

## Run the tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

The suite is offline. GitHub and model endpoints are simulated. Some tests need
`git`, and the sandbox tests need `bubblewrap`; they skip when it is missing.

## What a good change looks like

- Behaviour changes come with a test that fails without the change.
- Security-sensitive code (writes, paths, sandboxing, prompts) keeps the
  existing barriers. A prompt is not a control.
- Do not add a runtime dependency without discussing it first.
- Keep the no-merge and no-force-push boundaries intact.
- Do not put secrets, internal hostnames, or personal paths in the tree.
- Match the existing style; there is no formatter to run.

## Pull requests

- One topic per pull request.
- Describe the failure mode, the fix, and how you verified it.
- Say explicitly what is not tested.
- By contributing you agree to license your contribution under AGPL-3.0-or-later
  and to the Developer Certificate of Origin 1.1 (`git commit -s`).
