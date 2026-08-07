# Reviewing how this project gets worked on

Not part of the app. Tooling for looking back at the sessions themselves.

```bash
python3 .claude/workflow_review/extract.py            # newest session, readable
python3 .claude/workflow_review/extract.py --all      # every session
python3 .claude/workflow_review/extract.py --all --format csv > /tmp/turns.csv
```

Then open a **fresh** session and paste `ANALYSIS_PROMPT.md` into it.

## Why this reads the transcripts instead of keeping a log

Claude Code already records every prompt, every tool call, a timestamp on each,
and exact token usage per assistant message, in
`~/.claude/projects/<slug>/*.jsonl`. Everything a workflow review needs is in
there already.

Asking Claude to write a log entry each turn would be worse in four ways:

- **Two of the interesting numbers cannot be self-reported.** Claude does not
  observe how long its own reply took, and has no visibility into its token
  usage. Anything it wrote in those columns would be invented.
- **It would tax every turn.** A per-turn log costs tokens on each turn,
  including the many where nothing notable happened — making the thing being
  measured worse, and adding exactly the kind of overhead a review is meant to
  find.
- **It could not look backwards.** The transcripts already cover work done
  before any such convention existed.
- **Self-reported notes are the least trustworthy record available.** The
  harness's own log has no stake in how the work looked.

The one thing a live log could add that extraction cannot is commentary like
"this contradicts what was asked three turns ago". That is *analysis*, and the
reviewing session does it better: it can see how a decision actually turned out,
which nobody knew at the time.

## What is in the table

| column | meaning |
|---|---|
| `latency_s` | wall clock from prompt to last reply record — includes your own reading and typing when you interrupted |
| `tool_calls`, `tools` | how much machinery a turn needed |
| `files_touched`, `files` | churn; the same file across many turns is worth a look |
| `test_runs`, `git_commits` | verifying, versus thrashing |
| `thinking_chars` | how much reasoning a turn needed — a proxy for how underspecified it was |
| `in_tokens`, `cache_write_tokens`, `cache_read_tokens`, `out_tokens` | raw counts, not money |

Cache reads are far cheaper than cache writes or output. Adding the four columns
together and calling it "cost" would be wrong by a wide margin.

## The other record worth reading

`git log` on this project carries the reasoning for each decision, not just what
changed. The transcript shows what a conclusion cost to reach; the commit log
shows what the conclusion was. Reviewing them together is more informative than
either alone.
