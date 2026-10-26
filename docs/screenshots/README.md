# Demo assets

- `social-preview.png` — 1280×640 card built from `web/brand/fatchin-logo.png`.
  Upload it in **Settings → General → Social preview** (GitHub has no REST
  endpoint for this image).
- `demo.gif` / `demo.mp4` — generated from `scripts/demo-offline.py`, which
  runs the real worker against a real Git checkout with a simulated GitHub.
- `demo-terminal.png` — the final terminal frame.
- `tests.png` — a real run of the offline suite.

Regenerate the terminal capture and screenshots:

```sh
PYTHONPATH=src python3 scripts/demo-offline.py
python3 scripts/termshot.py "FatChin dry run — no GitHub write" /tmp/demo.clean.txt docs/screenshots/demo-terminal.png
```
