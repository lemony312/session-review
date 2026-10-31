#!/usr/bin/env python3
"""Open a review URL in the default browser, reusing or placing the tab smartly.

  open-review.py URL [--near w1,w2,...] [--window NAME]

On macOS, when the default https browser is a running Chromium browser with the
Chrome AppleScript dictionary (Brave, Chrome, Edge):
  1. a tab already showing URL is reloaded and focused;
  2. else a new tab opens in the window named NAME (--window);
  3. else in the window with the most tabs whose URL or title contains a --near word;
  4. else in the front window.
Anything else (other browsers, browser not running, Linux, osascript errors such as a
denied Automation permission) falls back to `open` / `xdg-open` / webbrowser.

Env: SESSION_REVIEW_BROWSER_ID overrides the detected bundle id (for testing).
Stdlib only. Prints one line saying what happened.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

LS_PLIST = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"

# Bundle id (lower-case) -> app name. All three ship the same Chromium scripting.sdef.
SUPPORTED = {
    "com.brave.browser": "Brave Browser",
    "com.google.chrome": "Google Chrome",
    "com.microsoft.edgemac": "Microsoft Edge",
}

OSASCRIPT_TIMEOUT = 60  # the first run waits on the Automation permission prompt

# Prints JSON: null if the app isn't running, else its windows front to back.
SNAPSHOT_JS = r"""
function run(argv) {
  const app = Application(argv[0]);
  if (!app.running()) return "null";
  return JSON.stringify(app.windows().map(w => ({
    id: String(w.id()),
    name: w.givenName() || "",
    mode: w.mode(),
    tabs: w.tabs().map(t => ({ url: t.url() || "", title: t.title() || "" })),
  })));
}
"""

# argv: bundle id, action (reuse|new), window id, tab index (0-based, reuse) or URL (new).
ACTION_JS = r"""
function run(argv) {
  const [bundle, action, wid, arg] = argv;
  const app = Application(bundle);
  const w = app.windows().find(x => String(x.id()) === wid);
  if (!w) throw new Error("window " + wid + " is gone");
  if (action === "reuse") {
    const i = Number(arg);
    w.tabs[i].reload();
    w.activeTabIndex = i + 1;
  } else {
    w.tabs.push(app.Tab({ url: arg }));
    w.activeTabIndex = w.tabs.length;
  }
  w.index = 1;
  app.activate();
  return "ok";
}
"""


def default_bundle_id(plist_path: Path = LS_PLIST) -> str | None:
    """Bundle id of the https handler from a LaunchServices plist (binary or XML), lower-cased."""
    try:
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    for h in data.get("LSHandlers", []) if isinstance(data, dict) else []:
        if isinstance(h, dict) and str(h.get("LSHandlerURLScheme", "")).lower() == "https":
            role = h.get("LSHandlerRoleAll")
            return str(role).lower() if role else None
    return None


def supported_app(bundle_id: str | None) -> str | None:
    return SUPPORTED.get((bundle_id or "").lower())


def parse_near(near: str | None) -> list[str]:
    """Comma-separated hints, lower-cased; words under 3 characters match too much and are dropped."""
    words = [w.strip().lower() for w in (near or "").split(",")]
    return list(dict.fromkeys(w for w in words if len(w) >= 3))


def choose(windows: list[dict], url: str, window_name: str | None = None, near: list[str] | None = None) -> dict | None:
    """Pick where URL goes. windows: front to back, each {id, name, mode, tabs: [{url, title}]}.

    Returns {"action": "reuse", "window", "tab"} or {"action": "new", "window", "why", "related"},
    or None when there is no window to use.
    """
    for w in windows:
        for i, t in enumerate(w.get("tabs", [])):
            if t.get("url") == url:
                return {"action": "reuse", "window": w["id"], "tab": i}

    normal = [w for w in windows if w.get("mode", "normal") != "incognito"]
    if not normal:
        return None

    if window_name:
        for w in normal:
            if (w.get("name") or "").lower() == window_name.lower():
                return {"action": "new", "window": w["id"], "why": "named", "related": 0}

    words = [w.lower() for w in (near or [])]
    if words:
        def related(w):
            return sum(any(x in (t.get("url", "") + "\n" + t.get("title", "")).lower() for x in words)
                       for t in w.get("tabs", []))
        best = max(normal, key=related)  # max keeps the front-most on ties
        n = related(best)
        if n:
            return {"action": "new", "window": best["id"], "why": "near", "related": n}

    return {"action": "new", "window": normal[0]["id"], "why": "front", "related": 0}


def describe(choice: dict, app: str, window_name: str | None) -> str:
    if choice["action"] == "reuse":
        return f"reused open tab in {app}"
    if choice["why"] == "named":
        return f"opened in {app} window \"{window_name}\""
    if choice["why"] == "near":
        n = choice["related"]
        return f"opened in {app} window with {n} related tab{'s' if n != 1 else ''}"
    return f"opened in {app} front window"


def osascript(script: str, *args: str) -> str:
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", script, *args],
                       capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT)
    if r.returncode != 0:
        lines = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"osascript exited {r.returncode}")
    return r.stdout.strip()


def smart_open(url: str, bundle_id: str, app: str, window_name: str | None, near: list[str]) -> str | None:
    """Open via AppleScript. Returns the status line, or None if the browser isn't running / has no windows."""
    windows = json.loads(osascript(SNAPSHOT_JS, bundle_id))
    if not windows:
        return None
    choice = choose(windows, url, window_name, near)
    if choice is None:
        return None
    arg = str(choice["tab"]) if choice["action"] == "reuse" else url
    osascript(ACTION_JS, bundle_id, "reuse" if choice["action"] == "reuse" else "new", choice["window"], arg)
    return describe(choice, app, window_name)


def plain_open(url: str) -> None:
    if sys.platform == "darwin" and shutil.which("open"):
        subprocess.run(["open", url], check=True)
    elif sys.platform.startswith("linux") and shutil.which("xdg-open"):
        subprocess.run(["xdg-open", url], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif not webbrowser.open(url):
        raise RuntimeError("no browser available")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Open a review URL, reusing or placing the tab smartly.")
    p.add_argument("url")
    p.add_argument("--near", help="comma-separated words: repo, branch, ticket key")
    p.add_argument("--window", help="open in the browser window with this name")
    a = p.parse_args(argv)

    note = ""
    if sys.platform == "darwin":
        bundle_id = (os.environ.get("SESSION_REVIEW_BROWSER_ID") or default_bundle_id() or "").lower()
        app = supported_app(bundle_id)
        if app:
            try:
                line = smart_open(a.url, bundle_id, app, a.window, parse_near(a.near))
                if line:
                    print(line)
                    return 0
                note = f" ({app} not running)"
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
                note = f" ({app} automation failed: {str(e)[:120]})"
    try:
        plain_open(a.url)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as e:
        print(f"could not open {a.url}: {e}", file=sys.stderr)
        return 1
    print("opened in default browser" + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
