# Background — why the review works this way

**You do not need this file to run a review.** Read `START_HERE.md` for that.
This one only explains the design.

## Files here

| file | audience |
|---|---|
| `START_HERE.md` | **you** — how to run a review |
| `ANALYSIS_PROMPT.md` | the reviewing session — it reads this itself |
| `extract.py` | the tool both of the above call |
| `turns.csv` | generated, gitignored, contains every prompt verbatim |

## Why it reads the session logs rather than keeping a diary

Claude Code already records every prompt, every tool call, a timestamp on each,
and exact token usage per assistant message, under
`~/.claude/projects/<slug>/*.jsonl`. Everything a workflow review needs is
already there.

Having Claude write a log entry each turn would have been worse in four ways:

- **Two of the wanted numbers cannot be self-reported.** Claude does not observe
  how long its own reply took, and has no visibility into its token usage.
  Anything written in those columns would have been invented.
- **It would tax every turn**, including the many where nothing notable
  happened — making the thing being measured worse, and adding exactly the kind
  of overhead a review exists to find.
- **It could not look backwards.** The logs already cover work done before any
  such convention existed.
- **Self-reported notes are the least trustworthy record available.** The
  harness's own log has no stake in how the work looked.

The one thing a live diary could add that extraction cannot is commentary like
"this contradicts what was asked three turns ago". That is *analysis*, and the
reviewing session does it better: it can see how a decision actually turned
out, which nobody knew at the time.

## What the table holds

| column | meaning |
|---|---|
| `latency_s` | wall clock, prompt to last reply — includes reading and typing time |
| `tool_calls`, `tools` | how much machinery a turn needed |
| `files_touched`, `files` | churn; the same file across many turns is worth a look |
| `test_runs`, `git_commits` | verifying, versus thrashing |
| `thinking_chars` | how much reasoning a turn needed — a proxy for how underspecified it was |
| `in_tokens`, `cache_write_tokens`, `cache_read_tokens`, `out_tokens` | raw counts, not money |

Cache reads are far cheaper than cache writes or output. Summing the four
columns and calling it "cost" would be wrong by a wide margin.

## The second record

`git log` on this project carries the reasoning for each decision, not just what
changed. The transcript shows what a conclusion cost to reach; the log shows
what it was. Reviewing them together is more informative than either alone.
