"""Local fixture-only six-viewport commercial release gate.

Install optional verification tools: pip install playwright pillow
Run with a local ghagent.web fixture (never a production instance):
  python scripts/verify-commercial-ui.py http://127.0.0.1:18763 /tmp/fatchin-proof
"""

import argparse
import io
import json
import sys
from urllib.parse import urlparse
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from pathlib import Path
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("url")
parser.add_argument("output", type=Path)
args = parser.parse_args()
if urlparse(args.url).hostname not in {"localhost", "127.0.0.1", "::1"}:
    parser.error("Use a local fixture, not a live account.")
OUT = args.output
OUT.mkdir(parents=True, exist_ok=True)
ROOT = Path(__file__).resolve().parents[1]
audit = (ROOT / "tests/commercial_ui_audit.js").read_text()
snapshot = json.loads((ROOT / "tests/fixtures/commercial-fleet.json").read_text())
views = [
    "overview",
    "jobs",
    "fleet",
    "learn",
    "repos",
    "watches",
    "doorbell",
    "fence",
    "settings",
    "help",
]
sizes = [
    (1440, 900, "01-desktop"),
    (1180, 820, "02-ipad-landscape"),
    (820, 1180, "03-ipad-portrait"),
    (390, 844, "04-iphone-portrait"),
    (844, 390, "05-iphone-landscape"),
    (932, 430, "06-iphone-landscape"),
]
results = []
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox"]
    )
    page = browser.new_page()
    # Seed populated fleet cards without ever dispatching a real worker.
    page.route("**/api/fleet", lambda route: route.fulfill(json=snapshot))
    lesson = json.loads((ROOT / "tests/fixtures/commercial-lesson.json").read_text())
    page.route("**/api/learn?*", lambda route: route.fulfill(json=lesson))
    page.goto(args.url)
    page.wait_for_selector("#breadcrumb strong")
    icon_results = []
    urls = [
        link.get_attribute("href")
        for link in page.locator(
            'link[rel="icon"],link[rel="apple-touch-icon"],link[rel="manifest"]'
        ).all()
    ]
    manifest = page.request.get(args.url.rstrip("/") + "/manifest.webmanifest").json()
    urls += [icon["src"] for icon in manifest["icons"]]
    urls += ["/brand/fatchin-logo.png"]
    for url in dict.fromkeys(urls):
        response = page.request.get(args.url.rstrip("/") + url)
        assert response.status == 200, (url, response.status)
        content = response.body()
        if url.endswith(".png"):
            im = Image.open(io.BytesIO(content))
            assert im.mode == "RGBA", url
            assert all(
                im.getpixel(c)[3] == 0
                for c in [
                    (0, 0),
                    (im.width - 1, 0),
                    (0, im.height - 1),
                    (im.width - 1, im.height - 1),
                ]
            ), url
        elif url.endswith(".webmanifest"):
            json.loads(content)
        elif url.endswith(".svg"):
            import xml.etree.ElementTree as ET

            assert ET.fromstring(content).tag.endswith("svg")
        elif url.endswith(".ico"):
            im = Image.open(io.BytesIO(content))
            assert im.ico.sizes() == {(16, 16), (32, 32), (48, 48)}
            for size in im.ico.sizes():
                frame = im.ico.getimage(size).convert("RGBA")
                assert frame.getpixel((0, 0))[3] == 0, (url, size)
        if url.endswith("fatchin-logo.png"):
            alpha = im.getchannel("A")
            mask = alpha.point(lambda a: 255 if a >= 128 else 0)
            ImageDraw.floodfill(mask, (0, 0), 128)
            # Everything not connected to the exterior is solid character ink.
            core = mask.point(lambda a: 0 if a == 128 else 255).filter(
                ImageFilter.MinFilter(5)
            )
            assert (
                ImageChops.subtract(core, alpha).getextrema()[1] == 0
            ), "Translucent interior ink"

        icon_results.append(
            {
                "url": url,
                "status": response.status,
                "type": response.headers["content-type"],
            }
        )
    (OUT / "icon-http-gate.json").write_text(json.dumps(icon_results, indent=2))
    for w, h, label in sizes:
        page.set_viewport_size({"width": w, "height": h})
        for view in views:
            page.goto(args.url.rstrip("/") + f"/#{view}")
            page.wait_for_selector("#breadcrumb strong")
            page.wait_for_timeout(450)
            page.evaluate("document.fonts.ready")
            issues = page.evaluate(audit)
            results.append({"viewport": [w, h], "view": view, **issues})
            if view == "fleet":
                for tab in ["agents", "ready", "comments", "activity"]:
                    page.locator("#fleet-tab-" + tab).click()
                    page.wait_for_timeout(100)
                    nested = page.evaluate(audit)
                    results.append(
                        {"viewport": [w, h], "view": "fleet/" + tab, **nested}
                    )
                    page.evaluate("window.scrollTo(0,0)")
                    page.screenshot(
                        path=str(OUT / f"{label}-{w}x{h}-fleet-{tab}.png"),
                        full_page=True,
                    )
            if view == "learn":
                for tab in page.locator('[data-action="explore-tab"]').all():
                    tab.click()
                    page.wait_for_timeout(100)
                    nested = page.evaluate(audit)
                    results.append(
                        {
                            "viewport": [w, h],
                            "view": "learn/" + tab.get_attribute("data-tab"),
                            **nested,
                        }
                    )
                page.evaluate(
                    "localStorage.setItem('fatchin-learning-session','commercial-lesson')"
                )
                page.goto(args.url.rstrip("/") + "/#learn")
                page.reload()
                page.wait_for_selector(".learn-session")
                page.evaluate("document.fonts.ready")
                populated = page.evaluate(audit)
                results.append(
                    {"viewport": [w, h], "view": "learn/populated-session", **populated}
                )
                page.screenshot(
                    path=str(OUT / f"{label}-{w}x{h}-learning-session.png"),
                    full_page=True,
                )
                page.evaluate("localStorage.removeItem('fatchin-learning-session')")
            if view == "overview":
                page.screenshot(path=str(OUT / f"{label}-{w}x{h}.png"), full_page=True)
            if any(issues.values()):
                print(w, h, view, {k: len(v) for k, v in issues.items() if v})
        if w <= 1000:
            page.goto(args.url.rstrip("/") + "/#overview")
            page.wait_for_selector("#breadcrumb strong")
            page.locator("#mobile-menu").click()
            page.wait_for_timeout(220)
            menu = page.evaluate(audit)
            results.append({"viewport": [w, h], "view": "navigation", **menu})
            page.screenshot(path=str(OUT / f"{label}-{w}x{h}-navigation.png"))
            page.keyboard.press("Escape")
    browser.close()
(OUT / "commercial-gate.json").write_text(json.dumps(results, indent=2))
print(
    "Total scenarios",
    len(results),
    "issues",
    sum(
        len(v)
        for r in results
        for k, v in r.items()
        if isinstance(v, list) and k != "viewport"
    ),
)

failures = [
    r for r in results if any(v for k, v in r.items() if k not in {"viewport", "view"})
]
if failures:
    print("FAILED scenarios:", [(r["viewport"], r["view"]) for r in failures])
    sys.exit(1)
print(
    "PASS: no collisions, clipping, contrast, control-edge, emoji, or page-width failures."
)
