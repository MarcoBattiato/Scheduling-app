#!/usr/bin/env python3
"""Turn Claude Code session transcripts into a table you can analyse.

The transcripts already record everything a workflow review needs — the prompts,
their timestamps, every tool call, and exact token counts per assistant message.
This reads them rather than asking Claude to keep a diary, which would be
self-reported, would cost tokens on every turn, and could not cover anything
that happened before the diary started.

    python3 .claude/workflow_review/extract.py                # newest session
    python3 .claude/workflow_review/extract.py --all          # every session
    python3 .claude/workflow_review/extract.py --format csv > turns.csv

Token counts are reported raw. Converting them to money needs current per-model
prices, which change — look them up rather than trusting a number baked in here.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional

PROJECTS = Path.home() / ".claude" / "projects"


def transcripts(project: Optional[Path], every: bool) -> List[Path]:
    root = project or _project_for(Path.cwd())
    found = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not found:
        raise SystemExit(f"no transcripts under {root}")
    return found if every else found[-1:]


def _project_for(cwd: Path) -> Path:
    """Claude Code names each project directory after its path."""
    slug = str(cwd).replace("/", "-")
    candidate = PROJECTS / slug
    if candidate.exists():
        return candidate
    matches = [p for p in PROJECTS.iterdir() if p.is_dir() and cwd.name in p.name]
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(
        f"cannot tell which project {cwd} is; pass --project explicitly.\n"
        f"available: {sorted(p.name for p in PROJECTS.iterdir() if p.is_dir())}"
    )


def records(path: Path) -> Iterator[dict]:
    for line in path.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _blocks(record: dict) -> List[dict]:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def is_prompt(record: dict) -> bool:
    """A thing the person typed — not a tool result, not harness bookkeeping."""
    if record.get("type") != "user" or record.get("isMeta"):
        return False
    blocks = _blocks(record)
    return bool(blocks) and not any(b.get("type") == "tool_result" for b in blocks)


def prompt_text(record: dict) -> str:
    parts = [b.get("text", "") for b in _blocks(record) if b.get("type") == "text"]
    images = sum(1 for b in _blocks(record) if b.get("type") == "image")
    text = " ".join(p.strip() for p in parts if p).strip()
    return (text + (f"  [+{images} image]" if images else "")) or "[no text]"


def when(record: dict) -> Optional[datetime]:
    stamp = record.get("timestamp")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def turns(path: Path) -> Iterator[dict]:
    """One row per prompt, covering everything up to the next prompt."""
    rows = [r for r in records(path) if r.get("type") in ("user", "assistant")]
    starts = [i for i, r in enumerate(rows) if is_prompt(r)]

    for n, start in enumerate(starts, 1):
        end = starts[n] if n < len(starts) else len(rows)
        span = rows[start:end]
        asked = rows[start]

        tokens = Counter()
        tools: Counter = Counter()
        files: set = set()
        commands: List[str] = []
        replied: List[datetime] = []
        text_out = 0
        thinking = 0

        for record in span[1:]:
            moment = when(record)
            if moment:
                replied.append(moment)
            usage = (record.get("message") or {}).get("usage") or {}
            for field in ("input_tokens", "cache_creation_input_tokens",
                          "cache_read_input_tokens", "output_tokens"):
                tokens[field] += usage.get(field, 0) or 0
            for block in _blocks(record):
                kind = block.get("type")
                if kind == "tool_use":
                    tools[block.get("name", "?")] += 1
                    payload = block.get("input") or {}
                    target = payload.get("file_path")
                    if target:
                        files.add(Path(target).name)
                    if block.get("name") == "Bash":
                        commands.append(payload.get("command", ""))
                elif kind == "text":
                    text_out += len(block.get("text", ""))
                elif kind == "thinking":
                    thinking += len(block.get("thinking", ""))

        asked_at = when(asked)
        latency = (
            round((max(replied) - asked_at).total_seconds(), 1)
            if asked_at and replied else None
        )

        yield {
            "turn": n,
            "session": path.stem[:8],
            "asked_at": asked_at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if asked_at else "",
            "latency_s": latency,
            "prompt_chars": len(prompt_text(asked)),
            "prompt": prompt_text(asked),
            "tool_calls": sum(tools.values()),
            "tools": ", ".join(f"{k}×{v}" for k, v in tools.most_common()),
            "files_touched": len(files),
            "files": ", ".join(sorted(files)),
            # Cheap signals for the analysis: a turn that ran tests was
            # probably verifying, one that ran many is probably thrashing.
            "test_runs": sum(1 for c in commands if "pytest" in c),
            "git_commits": sum(1 for c in commands if "git commit" in c),
            "reply_chars": text_out,
            "thinking_chars": thinking,
            "in_tokens": tokens["input_tokens"],
            "cache_write_tokens": tokens["cache_creation_input_tokens"],
            "cache_read_tokens": tokens["cache_read_input_tokens"],
            "out_tokens": tokens["output_tokens"],
        }


def show_turn(paths: List[Path], wanted: int) -> None:
    """One exchange, in full. Reading a whole transcript is not an option at
    several megabytes, so this is the way to look at a turn closely."""
    n = 0
    for path in paths:
        rows = [r for r in records(path) if r.get("type") in ("user", "assistant")]
        starts = [i for i, r in enumerate(rows) if is_prompt(r)]
        for k, start in enumerate(starts):
            n += 1
            if n != wanted:
                continue
            end = starts[k + 1] if k + 1 < len(starts) else len(rows)
            print(f"=== turn {n} · {path.stem[:8]} · {rows[start].get('timestamp', '')}\n")
            print("--- asked ---")
            print(prompt_text(rows[start]))
            for record in rows[start + 1:end]:
                for block in _blocks(record):
                    kind = block.get("type")
                    if kind == "text" and block.get("text", "").strip():
                        print(f"\n--- replied ---\n{block['text']}")
                    elif kind == "tool_use":
                        payload = block.get("input") or {}
                        detail = (payload.get("command")
                                  or payload.get("file_path")
                                  or payload.get("pattern") or "")
                        print(f"\n[tool] {block.get('name')}: {str(detail)[:160]}")
                    elif kind == "tool_result":
                        pass
            return
    raise SystemExit(f"no turn {wanted}; there are {n}")


FIELDS = ["turn", "session", "asked_at", "latency_s", "prompt_chars", "tool_calls",
          "tools", "files_touched", "files", "test_runs", "git_commits",
          "reply_chars", "thinking_chars",
          "in_tokens", "cache_write_tokens", "cache_read_tokens", "out_tokens",
          "prompt"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=Path, help="a ~/.claude/projects/<slug> directory")
    parser.add_argument("--all", action="store_true", help="every session, not just the newest")
    parser.add_argument("--format", choices=("table", "csv", "jsonl"), default="table")
    parser.add_argument("--full-prompts", action="store_true",
                        help="do not truncate prompts (csv/jsonl always keep them whole)")
    parser.add_argument("--turn", type=int, metavar="N",
                        help="print one turn in full — the prompt, the reply, and "
                             "every tool call. The transcripts are far too large to "
                             "read whole, so this is how to quote a specific exchange.")
    args = parser.parse_args()

    paths = transcripts(args.project, args.all)
    if args.turn:
        show_turn(paths, args.turn)
        return

    rows = [row for path in transcripts(args.project, args.all) for row in turns(path)]
    if not rows:
        raise SystemExit("no prompts found in those transcripts")

    if args.format == "csv":
        out = csv.DictWriter(sys.stdout, fieldnames=FIELDS, extrasaction="ignore")
        out.writeheader()
        out.writerows(rows)
        return
    if args.format == "jsonl":
        for row in rows:
            print(json.dumps(row))
        return

    total = Counter()
    print(f"{'#':>4} {'when':<17} {'secs':>6} {'tools':>6} {'files':>6} "
          f"{'out':>7} {'cache-r':>8}  prompt")
    print("-" * 110)
    for row in rows:
        for field in ("in_tokens", "cache_write_tokens", "cache_read_tokens", "out_tokens"):
            total[field] += row[field]
        prompt = row["prompt"].replace("\n", " ")
        if not args.full_prompts and len(prompt) > 46:
            prompt = prompt[:45] + "…"
        print(f"{row['turn']:>4} {row['asked_at'][5:]:<17} "
              f"{(row['latency_s'] or 0):>6.0f} {row['tool_calls']:>6} "
              f"{row['files_touched']:>6} {row['out_tokens']:>7} "
              f"{row['cache_read_tokens']:>8}  {prompt}")

    print("-" * 110)
    print(f"{len(rows)} turns · output {total['out_tokens']:,} · "
          f"input {total['in_tokens']:,} · cache written {total['cache_write_tokens']:,} · "
          f"cache read {total['cache_read_tokens']:,}")
    print("\nToken counts are raw. Prices change — look up current ones rather than\n"
          "assuming; cache reads are far cheaper than writes, which matters here.")


if __name__ == "__main__":
    main()
