# Dependencies

FatChin is deliberately **zero-runtime-dependency**: the worker, queue, web
server, and interface use the Python standard library and local static assets.
There is no `pip install` package to compromise at run time, no npm tree, and
no CDN or third-party script fetched by the browser. There is **no
`package.json`, no npm install, no Node version pin, and no frontend build
step**; the browser code is plain JavaScript served as static files.

That does not mean there is no dependency surface. This page lists what is
actually trusted and how it is kept current.

## The real surface

| Component | Version used | How it is controlled |
| --- | --- | --- |
| Python | 3.11 through 3.15 | CI matrix runs the suite on every supported minor; `requires-python = ">=3.11"` |
| Build backend | `setuptools==84.2.1` (exact) | Pinned in `pyproject.toml`; the package job installs that exact version and builds without isolation |
| Git | system package | Documented minimum; exercised by the suite |
| `gh` CLI | system package | Documented minimum; authenticated by the operator |
| bubblewrap | system package | Required for isolated checks; missing or unusable means commands are refused |
| GitHub Actions | `checkout` v7.0.1, `setup-python` v7.1.0 | Pinned by full commit SHA with a version comment; Dependabot proposes updates |
| Runner image | Ubuntu 24.04 pinned; Ubuntu 26.04 canary on Python 3.15 | The canary proves the next image before `ubuntu-latest` migrates |
| Browser assets | Inter font, local SVG icons | Served from `web/`; no network request at runtime |
| Node.js | none at runtime | The worker and Python suite never invoke Node. The optional Chrome/CDP runners use the machine's current Node and are not installed by this project. |

## Update policy

1. **No new runtime dependency without an explicit decision.** If one is
   proposed, it needs a named reason, a licence check, and a supply-chain
   review first.
2. **Python support is proved, not claimed.** A new minor is added to the CI
   matrix before it is listed in the classifiers. The current line is
   3.11–3.15.
3. **Actions are pinned by commit SHA.** Tags are mutable. Dependabot opens a
   grouped pull request for action updates; the SHA and version comment are
   updated together.
4. **The build backend is exact-pinned.** `setuptools==84.2.1` is installed
   explicitly in the packaging job. Do not relax it to a range.
5. **System tools are documented, not bundled.** Git, `gh`, and bubblewrap
   are operator-managed. The worker refuses to run checks without a working
   sandbox rather than falling back.
6. **Vendored assets record provenance.** The logo is recorded with its
   source and SHA-256 in `web/brand/README.md`; fonts and icons carry their
   licences beside them.
7. **Dependabot and alerts stay on.** Vulnerability alerts and automated
   security fixes are enabled on the repository. Action updates are grouped
   weekly; pip metadata is checked monthly.
8. **Review the surface monthly.** Check Python end-of-life dates, the latest
   stable action releases, and the pinned setuptools version. Update, run the
   matrix, and record the change.

## Verification

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

The CI workflow runs that suite on Python 3.11, 3.12, 3.13, 3.14, and 3.15,
then installs the package with the pinned build backend and confirms both
entry points. It does not fetch a runtime dependency because there is none.

## If dependencies are ever added

Add `pip-audit` (or an equivalent) to CI, generate an SBOM for the release,
and record each new package with its purpose, licence, and version. Do not add
a dependency merely for convenience; the standard library is the default.
