#!/usr/bin/env python3
"""Build the README clips in docs/media/. Dev-only.

    python docs/demo/build_demo.py              all five clips
    python docs/demo/build_demo.py --only apply one of: overview digest dream review apply
    python docs/demo/build_demo.py --frames DIR also save every frame as a PNG, to look at

The tool itself needs nothing but Python. This script additionally needs Pillow and
Playwright (`pip install pillow playwright`) and an installed Chrome or Edge, which
Playwright drives directly; no browser download is needed.

What is real and what is drawn:
  - Every number, file name and line of output comes from running the real dreamctl over
    the made-up project in demo_world.py.
  - review.gif is the real review page, served by the real server, clicked for real.
  - overview, digest, dream and apply are drawn pages (scenes.html) fed with that data.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import demo_world  # noqa: E402

try:
    from PIL import Image
    from playwright.sync_api import sync_playwright
except ImportError as ex:   # pragma: no cover
    sys.exit(f"build_demo.py needs Pillow and Playwright ({ex}). The tool itself does not.")

W, H = 900, 520
SCENES = ("overview", "digest", "dream", "apply")
CURSOR = """
(() => {
  const c = document.createElement('div');
  c.id = 'demo-cursor';
  c.innerHTML = '<svg width="22" height="26" viewBox="0 0 22 26"><path d="M2 1 L2 20 L7 15.5 L10.5 24 L14 22.5 L10.5 14.5 L17.5 14.5 Z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  c.style.cssText = 'position:fixed;left:450px;top:300px;z-index:99999;pointer-events:none;';
  const ring = document.createElement('div');
  ring.id = 'demo-ring';
  ring.style.cssText = 'position:fixed;width:34px;height:34px;margin:-16px 0 0 -15px;border:3px solid #4f46e5;border-radius:50%;z-index:99998;pointer-events:none;display:none;';
  document.body.append(ring, c);
  window.demoCursor = (x, y) => { c.style.left = x + 'px'; c.style.top = y + 'px'; ring.style.left = x + 'px'; ring.style.top = y + 'px'; };
  window.demoRing = (on) => { ring.style.display = on ? 'block' : 'none'; };
})();
"""


def launch(pw):
    """Use a browser that is already installed; Playwright's own download is not required."""
    last = None
    for channel in ("msedge", "chrome", None):
        try:
            return pw.chromium.launch(channel=channel, headless=True) if channel else pw.chromium.launch(headless=True)
        except Exception as ex:   # noqa: BLE001
            last = ex
    sys.exit(f"no usable browser for Playwright: {last}")


def shot(page) -> Image.Image:
    return Image.open(io.BytesIO(page.screenshot())).convert("RGB")


def drawn_scene(browser, name: str, story: dict) -> list[tuple[Image.Image, int]]:
    ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1, color_scheme="light")
    page = ctx.new_page()
    page.goto((HERE / "scenes.html").as_uri())
    page.set_viewport_size({"width": W, "height": page.evaluate("n => sceneHeight(n)", name)})
    steps = page.evaluate("([n, d]) => start(n, d)", [name, story])
    frames = []
    for i in range(steps):
        hold = page.evaluate("i => step(i)", i)
        frames.append((shot(page), int(hold)))
    ctx.close()
    return frames


def review_scene(browser, world: demo_world.World, rep: Path) -> list[tuple[Image.Image, int]]:
    """Open the real review page and click through it: accept, accept, reject, edit, finish."""
    import os
    env = dict(os.environ, HOME=str(world.home), USERPROFILE=str(world.home))
    srv = subprocess.Popen([sys.executable, str(demo_world.DREAMCTL), "--root", str(world.root), "serve", str(rep), "--no-browser"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    frames: list[tuple[Image.Image, int]] = []
    try:
        url = re.search(r"http://127\.0\.0\.1:\d+/", srv.stdout.readline()).group(0)
        ctx = browser.new_context(viewport={"width": W, "height": 600}, device_scale_factor=1, color_scheme="light")
        page = ctx.new_page()
        page.goto(url)
        page.wait_for_selector("#card-p004")
        page.evaluate(CURSOR)
        pos = [450.0, 300.0]

        def snap(ms: int) -> None:
            frames.append((shot(page), ms))

        def move_to(selector: str) -> None:
            box = page.locator(selector).first.bounding_box()
            tx, ty = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            for k in range(1, 6):
                t = k / 5
                t = t * t * (3 - 2 * t)
                page.evaluate("([x, y]) => demoCursor(x, y)", [pos[0] + (tx - pos[0]) * t, pos[1] + (ty - pos[1]) * t])
                snap(70)
            pos[:] = [tx, ty]

        def click(selector: str, after: int) -> None:
            move_to(selector)
            page.evaluate("demoRing(true)")
            snap(160)
            page.locator(selector).first.click()
            page.evaluate("demoRing(false)")
            page.wait_for_timeout(120)   # the page posts the decision and redraws the card
            snap(after)

        def scroll_to(selector: str, offset: int = 70) -> None:
            y0 = page.evaluate("window.scrollY")
            y1 = max(0, page.evaluate("s => document.querySelector(s).getBoundingClientRect().top + window.scrollY", selector) - offset)
            for k in range(1, 5):
                page.evaluate("y => window.scrollTo(0, y)", y0 + (y1 - y0) * k / 4)
                snap(70)
            snap(320)

        snap(1400)
        click("#card-p001 button[data-act=accept]", 800)
        scroll_to("#card-p002")
        click("#card-p002 button[data-act=accept]", 800)
        scroll_to("#card-p003")
        click("#card-p003 button[data-act=reject]", 900)
        scroll_to("#card-p004")
        click("#card-p004 button[data-act=edit]", 500)
        scroll_to("#edit-p004", 150)
        page.evaluate("() => { const t = document.getElementById('ta-p004'); t.scrollTop = t.scrollHeight; }")   # bring the line to edit into view
        snap(350)
        old = "Ask first, say what will be removed, and wait for a yes."
        add = " If the tool has a dry run, offer it."
        for k in range(1, 5):   # the edit appears a few words at a time
            part = add[: round(len(add) * k / 4)]
            page.evaluate("([o, n]) => { const t = document.getElementById('ta-p004'); t.value = t.value.replace(o, n); }",
                          [old + add[: round(len(add) * (k - 1) / 4)], old + part])
            snap(220)
        snap(500)
        click("#card-p004 button[data-act=saveedit]", 900)
        click("#finish", 3000)
        ctx.close()
        srv.wait(timeout=20)
    finally:
        if srv.poll() is None:
            srv.kill()
    return frames


def save_gif(frames: list[tuple[Image.Image, int]], path: Path, colors: int) -> None:
    """One shared palette, no dithering: flat pages stay crisp and only changed areas are stored."""
    merged: list[list] = []
    for im, ms in frames:   # identical neighbours become one longer frame
        if merged and list(merged[-1][0].getdata()) == list(im.getdata()):
            merged[-1][1] += ms
        else:
            merged.append([im, ms])
    w, h = merged[0][0].size
    sample = merged[:: max(1, len(merged) // 10)]
    sheet = Image.new("RGB", (w, h * len(sample)))
    for k, (im, _) in enumerate(sample):
        sheet.paste(im, (0, k * h))
    palette = sheet.quantize(colors=colors, method=Image.MEDIANCUT, dither=Image.NONE)
    quant = [im.quantize(palette=palette, dither=Image.NONE) for im, _ in merged]
    path.parent.mkdir(parents=True, exist_ok=True)
    quant[0].save(path, save_all=True, append_images=quant[1:], duration=[ms for _, ms in merged], loop=0, optimize=False, disposal=1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="comma-separated clips to build (default: all)")
    ap.add_argument("--out", default=str(HERE.parent / "media"))
    ap.add_argument("--frames", help="also write every frame as PNG under this folder")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway demo folder and print where it is")
    args = ap.parse_args()
    want = set((args.only or "overview,digest,dream,review,apply").split(","))
    out = Path(args.out)

    base = Path(tempfile.mkdtemp(prefix="recensio-demo-"))
    world = demo_world.World(base)
    try:
        world.build()
        world.digest()
        rep = world.dream()
        clips: dict[str, list] = {}
        with sync_playwright() as pw:
            browser = launch(pw)
            review = review_scene(browser, world, rep)   # always: its clicks are the decisions apply acts on
            world.apply(rep)
            story = world.story
            leak = re.search(re.escape(str(Path.home().name)) + r"|[A-Za-z]:[\\/]Users|/Users/|/home/(?!sam/)", json.dumps(story))
            if leak:
                sys.exit(f"the demo story contains a real path ({leak.group(0)!r}); not building clips from it")
            if "review" in want:
                clips["review"] = review
            for name in SCENES:
                if name in want:
                    clips[name] = drawn_scene(browser, name, story)
            browser.close()
        for name, frames in clips.items():
            save_gif(frames, out / f"{name}.gif", colors=64 if name != "review" else 96)
            secs = sum(ms for _, ms in frames) / 1000
            print(f"{name}.gif  {len(frames)} frames  {secs:.1f} s  {(out / (name + '.gif')).stat().st_size / 1e6:.2f} MB")
            if args.frames:
                d = Path(args.frames) / name
                d.mkdir(parents=True, exist_ok=True)
                for k, (im, _) in enumerate(frames):
                    im.save(d / f"{k:03d}.png")
        if args.frames:   # the captured demo data the clips were drawn from, for the record
            (Path(args.frames) / "story.json").write_text(json.dumps(story, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    finally:
        if args.keep:
            print("demo folder kept at", base)
        else:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
