#!/usr/bin/env python3
"""Write the build log from the git history: one line per day of work.

    python3 tools/showcase/buildlog.py

Rewrites showcase/BUILDLOG.md in full, and the "Latest builds" block in the
main README (between the buildlog:latest markers) with the five newest days.
Run it whenever the showcase is refreshed; it is deterministic, so running it
twice changes nothing.

Each line says WHICH KIND of work landed, from the paths that changed, never
from commit messages or file contents. A path that matches no rule is counted
silently, never printed. A new top-level kind of file should get a rule in
classify(), or it will not show up at all.

The build number is the number of commits in the history up to that day.
"""
import os
import re
import subprocess
from collections import OrderedDict

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOG = os.path.join(REPO, "showcase", "BUILDLOG.md")
README = os.path.join(REPO, "README.md")
LATEST = ("<!-- buildlog:latest:start -->", "<!-- buildlog:latest:end -->")
AREAS = ("App", "Assistant", "Payments", "Calendar sync", "Scheduling engine", "Calendar store",
         "Permissions", "Example data", "Testing", "Design notes", "Showcase")


def git(*args):
    return subprocess.run(("git", "-C", REPO) + args, capture_output=True, text=True,
                          check=True).stdout


def classify(paths):
    """paths -> OrderedDict(area -> set of details)."""
    out = OrderedDict((a, set()) for a in AREAS)
    for p in paths:
        static = "mock_ui/src/mock_ui/static/"
        name = os.path.basename(p)
        if "/tests/" in p or p.startswith("tests/"):
            out["Testing"].add("")
        elif p.startswith(static):
            if name.startswith("phone"):
                out["App"].add("phone screens")
            elif name == "tokens.css":
                out["App"].add("design tokens")
            elif name.startswith("icon"):
                out["App"].add("icon")
            else:
                out["App"].add("desktop view")
        elif p == "mock_ui/src/mock_ui/assistant.py":
            out["Assistant"].add("")
        elif p in ("mock_ui/src/mock_ui/billing.py", "mock_ui/src/mock_ui/payments.py"):
            out["Payments"].add("")
        elif p == "mock_ui/src/mock_ui/calendar_sync.py":
            out["Calendar sync"].add("")
        elif p == "mock_ui/src/mock_ui/scenarios.py":
            out["Example data"].add("")
        elif p.startswith("mock_ui/src/"):
            out["App"].add("server")
        elif p.startswith("scheduling_engine/src/"):
            out["Scheduling engine"].add("")
        elif p.startswith("calendar_store/src/"):
            out["Calendar store"].add("")
        elif p.startswith("scheduling_contracts/src/"):
            out["Permissions"].add("")
        elif p.startswith(("showcase/", "tools/showcase/")):
            out["Showcase"].add("")
        elif p.startswith("docs/") or p.endswith(".md"):
            out["Design notes"].add("")
    return out


def fmt(name, details):
    d = sorted(x for x in details if x)
    return f"**{name}**" + (f": {', '.join(d)}" if d else "")


def days():
    """[(date, build, commits, line)] newest first."""
    rows = git("log", "--reverse", "--format=%H %ad", "--date=short").split("\n")
    by_day = OrderedDict()
    for n, row in enumerate(r for r in rows if r):
        sha, day = row.split(" ")
        by_day.setdefault(day, []).append((n + 1, sha))
    out = []
    for day, commits in by_day.items():
        first, last = commits[0][1], commits[-1][1]
        parent = git("rev-list", "--parents", "-n", "1", first).split()[1:]
        base = parent[0] if parent else git("hash-object", "-t", "tree", "/dev/null").strip()
        paths = [p for p in git("diff", "--name-only", base, last).split("\n") if p]
        bits = [fmt(a, d) for a, d in classify(paths).items() if d] or ["housekeeping"]
        count = len(commits)
        line = f"`build {commits[-1][0]}` · {' · '.join(bits)} — {count} commit{'s' * (count != 1)}"
        out.append((day, line))
    return list(reversed(out))


def main():
    entries = days()
    body = ["# Build log", "",
            "Every day of work on the app, newest first, rebuilt from the git history by",
            "`tools/showcase/buildlog.py`. Each line says what kind of work landed; the build",
            "number is the number of commits so far. The main [README](../README.md) shows",
            "the latest five.", ""]
    for day, line in entries:
        body += [f"### {day}", "", f"- {line}", ""]
    with open(LOG, "w") as f:
        f.write("\n".join(body))

    latest = "\n".join(f"- {day} {line}" for day, line in entries[:5])
    text = open(README).read()
    a, b = text.find(LATEST[0]), text.find(LATEST[1])
    if a < 0 or b < a:
        raise SystemExit("README.md has no buildlog:latest markers")
    text = text[:a + len(LATEST[0])] + "\n" + latest + "\n" + text[b:]
    with open(README, "w") as f:
        f.write(text)
    print(latest)


if __name__ == "__main__":
    main()
