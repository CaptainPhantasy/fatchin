# Security policy

FatChin runs untrusted repository content through a language model and can
write to GitHub on your behalf. Treat it as an administrative tool.

## Reporting a vulnerability

Open a private security advisory on this repository
(`Security` → `Report a vulnerability`). Do not open a public issue for a
vulnerability. Include the affected version, a reproduction, and the impact.

## Default posture

- The web interface binds to `127.0.0.1` by default. Binding beyond loopback
  requires an admin token; the server refuses to start without one.
- `/api/*` requires `Authorization: Bearer <token>` when a token is configured.
- There is **no merge code path**. The fleet can mark a pull request ready; it
  cannot merge, approve, or force-push.
- Dry run records intended writes and sends nothing.
- Repository policy keeps organisation namespaces out until named.
- Checks and model-issued commands run in bubblewrap with no network, no
  account files, and read-only Git metadata. If bubblewrap is missing the
  command is refused rather than run unconfined.

## Known limitations

- The store is a single JSON document with a global lock. It is designed for
  one trusted operator on one machine, not multi-tenant isolation.
- The operator controls the configuration file and the GitHub token. A person
  with shell access can change what the worker may do.
- Model output is untrusted. The capability and write barriers are the security
  boundary; the prompt is not.
- GitHub tokens and model credentials are read from an environment file. Use
  file permissions appropriate for your host.
- Only GitHub is supported.

## Supported versions

Security fixes are applied to `main`. There are no maintained release branches
yet.
