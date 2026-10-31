"""Tests for scripts/open-review.py: default-browser detection, supported browsers, window choice.

Run:  uv run python test_open_review.py   (no browser or osascript needed)
"""
from __future__ import annotations

import importlib.util
import plistlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("open_review", ROOT / "scripts" / "open-review.py")
opener = importlib.util.module_from_spec(spec)
spec.loader.exec_module(opener)

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  {detail}"))
    fails += not ok


# --- default browser from a LaunchServices plist --------------------------------------
handlers = {"LSHandlers": [
    {"LSHandlerContentType": "public.html", "LSHandlerRoleAll": "com.apple.safari"},
    {"LSHandlerURLScheme": "mailto", "LSHandlerRoleAll": "com.apple.mail"},
    {"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "com.brave.Browser",
     "LSHandlerPreferredVersions": {"LSHandlerRoleAll": "-"}},
]}
with tempfile.TemporaryDirectory() as d:
    for fmt, label in ((plistlib.FMT_BINARY, "binary"), (plistlib.FMT_XML, "xml")):
        f = Path(d) / f"ls-{label}.plist"
        f.write_bytes(plistlib.dumps(handlers, fmt=fmt))
        got = opener.default_bundle_id(f)
        check(f"https handler read from {label} plist (lower-cased)", got == "com.brave.browser", got)
    none = Path(d) / "none.plist"
    none.write_bytes(plistlib.dumps({"LSHandlers": [{"LSHandlerURLScheme": "mailto", "LSHandlerRoleAll": "x"}]}))
    check("no https handler -> None", opener.default_bundle_id(none) is None)
    junk = Path(d) / "junk.plist"
    junk.write_bytes(b"not a plist")
    check("unreadable plist -> None", opener.default_bundle_id(junk) is None)
    check("missing plist -> None", opener.default_bundle_id(Path(d) / "missing.plist") is None)

# --- supported browsers ---------------------------------------------------------------
check("Brave supported", opener.supported_app("com.brave.Browser") == "Brave Browser")
check("Chrome supported, case-insensitive", opener.supported_app("com.google.Chrome") == "Google Chrome")
check("Edge supported", opener.supported_app("com.microsoft.edgemac") == "Microsoft Edge")
for bid in ("com.apple.safari", "org.mozilla.firefox", "company.thebrowser.browser", "", None):
    check(f"{bid!r} not supported", opener.supported_app(bid) is None)

# --- near hints -----------------------------------------------------------------------
check("near: split, lower-case, dedupe, drop short",
      opener.parse_near(" my-app,PROJ-123,,proj-123,ui ") == ["my-app", "proj-123"],
      opener.parse_near(" my-app,PROJ-123,,proj-123,ui "))
check("near: none -> []", opener.parse_near(None) == [])

# --- window choice --------------------------------------------------------------------
URL = "http://localhost:8087/review/7"


def win(wid, name="", tabs=(), mode="normal"):
    return {"id": wid, "name": name, "mode": mode,
            "tabs": [{"url": u, "title": t} for u, t in tabs]}


front = win("1", tabs=[("https://news.example", "News")])
reviews_win = win("2", "Reviews", [("https://github.com/acme/my-app/pull/1", "PR 1"),
                           ("https://jira.example/browse/PROJ-123", "PROJ-123 fix it")])
other = win("3", "Work", [("https://github.com/acme/sdk", "sdk"),
                          ("https://ci.example/my-app", "my-app build"),
                          ("https://ci.example/run/2", "Build for my-app"),
                          ("https://ci.example/run/3", "unrelated")])
with_review = win("4", tabs=[("https://a", "a"), (URL, "Review")])

c = opener.choose([front, reviews_win, with_review], URL, "Reviews", ["my-app"])
check("exact URL reused, ahead of name and near", c == {"action": "reuse", "window": "4", "tab": 1}, c)
c = opener.choose([front, win("5", tabs=[(URL + "#x", "")])], URL)
check("URL with a different fragment is not an exact match", c["action"] == "new", c)

c = opener.choose([front, reviews_win, other], URL, "reviews", ["my-app"])
check("window name matched case-insensitively, ahead of near",
      c["action"] == "new" and c["window"] == "2" and c["why"] == "named", c)
c = opener.choose([front, reviews_win, other], URL, "Nope", ["my-app"])
check("unknown window name falls through to near", c["why"] == "near" and c["window"] == "3", c)

c = opener.choose([front, reviews_win, other], URL, None, ["my-app"])
check("near picks the window with the most related tabs (url or title)",
      c == {"action": "new", "window": "3", "why": "near", "related": 2}, c)
c = opener.choose([front, reviews_win, other], URL, None, ["proj-123", "fix it", "acme/my-app"])
check("near: any word counts, a tab counts once", c["window"] == "2" and c["related"] == 2, c)
c = opener.choose([front, reviews_win, win("6", tabs=[("https://x/PROJ-123", "")])], URL, None, ["proj-123"])
check("near tie keeps the front-most window", c["window"] == "2" and c["related"] == 1, c)

c = opener.choose([front, reviews_win], URL, None, ["nothing-matches"])
check("no related tabs -> front window", c == {"action": "new", "window": "1", "why": "front", "related": 0}, c)
c = opener.choose([front, reviews_win], URL)
check("no hints -> front window", c["window"] == "1" and c["why"] == "front", c)

incog = win("9", "Reviews", [("https://github.com/acme/my-app", "")], mode="incognito")
c = opener.choose([incog, front], URL, "Reviews", ["my-app"])
check("incognito windows are never chosen for a new tab", c["window"] == "1" and c["why"] == "front", c)
check("only incognito windows -> None (plain open)", opener.choose([incog], URL) is None)
check("no windows -> None (plain open)", opener.choose([], URL) is None)

# --- status lines ---------------------------------------------------------------------
check("describe reuse", opener.describe({"action": "reuse"}, "Brave Browser", None) == "reused open tab in Brave Browser")
check("describe near", opener.describe({"action": "new", "why": "near", "related": 3}, "Brave Browser", None)
      == "opened in Brave Browser window with 3 related tabs")
check("describe named", opener.describe({"action": "new", "why": "named", "related": 0}, "Brave Browser", "Reviews")
      == 'opened in Brave Browser window "Reviews"')

sys.exit(1 if fails else 0)
