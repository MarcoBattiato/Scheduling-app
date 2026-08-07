# Workflow review — brief for the reviewing session

**You are reading this because you were asked to review how this project has
been worked on.** Not the code: the collaboration. Carry out what follows.

The person who asked wants to improve their own prompting and workflow. Be
critical. Findings that make them look inefficient are the useful ones;
reassurance is not what this is for.

---

## What happened here, in one paragraph

This repository holds a scheduling system built over several sessions: a
`calendar_store` package for availability and appointments, a
`scheduling_engine` that places bookings with a CP-SAT solver, and a `mock_ui`
for driving it by hand. Work proceeded conversationally — design discussion,
then implementation, then correction. Several design decisions were revisited
after being built. That pattern is the main thing to examine.

## What to read, and how

Everything is reachable from this directory. Nothing was attached.

**1. The table — read this whole.** About 50 KB, one row per prompt.

```
.claude/workflow_review/turns.csv
```

Columns: `turn`, `asked_at`, `latency_s`, `prompt_chars`, `tool_calls`,
`tools`, `files_touched`, `files`, `test_runs`, `git_commits`, `reply_chars`,
`thinking_chars`, `in_tokens`, `cache_write_tokens`, `cache_read_tokens`,
`out_tokens`, `prompt`.

If it is missing or stale, rebuild it:

```
python3 .claude/workflow_review/extract.py --all --format csv \
    > .claude/workflow_review/turns.csv
```

**2. Individual exchanges — one at a time.** The raw transcripts total nearly
8 MB. **Do not read them whole**; you will exhaust your context before reaching
a conclusion. Use the table to choose which turns matter, then:

```
python3 .claude/workflow_review/extract.py --all --turn 26
```

That prints the prompt, the reply, and every tool call for that turn. Budget
yourself to roughly fifteen or twenty of these.

**3. `git log`.** The commit messages here carry the reasoning behind each
decision, not just what changed. The transcript shows what a conclusion cost to
reach; the log shows what the conclusion was. Reading both is more informative
than either.

## What to look for

Roughly in order of value.

1. **Prompts that caused rework.** Places where something was built, then
   changed or undone a few turns later because the instruction was ambiguous,
   incomplete, or contradicted something earlier. Quote both prompts and give
   the wording that would have avoided it. Spend most of your effort here.

2. **Where the tokens went.** Which turns were expensive, and was the spend
   justified by what came out? Separate expensive-and-productive from
   expensive-and-wasted. Look for repeated reading of the same files, long tool
   chains ending in a small change, and work later discarded.

3. **Reversals**, and whether each was caused by new information — unavoidable,
   fine — or by not having thought it through before asking. Say which.

4. **Too little context, and too much.** Both cost. Point at specific prompts.

5. **Tools and features that went unused but would have helped.** Plan mode,
   subagents for parallel investigation, `/code-review`, custom slash commands
   for repeated instructions, hooks for things asked for by hand more than
   once, `CLAUDE.md` for standing preferences restated often, memory for facts
   repeated. Only name ones that would genuinely have helped *this* work, and
   say at which turn.

6. **Anything repeated more than twice.** Repetition is the clearest sign that
   something belongs in configuration rather than in a prompt.

## How to answer

Lead with the three or four findings that would save the most time or tokens,
each with a concrete before/after rewrite of one of their actual prompts. Then
the supporting detail. Keep it short enough to act on — a long report that gets
skimmed is worse than a short one that gets used.

## Cautions

- **`latency_s` is wall clock**, prompt to last reply record. It includes the
  person's own reading and typing time when they interrupted, and gaps where
  they walked away. Treat large values as suspect, not as thinking time.
- **Token counts are raw.** Cache reads are far cheaper than cache writes or
  output; do not sum the four columns and call it cost. Look up current pricing
  if you want money figures rather than assuming a rate.
- **Some turns begin with `<ide_opened_file>` or `<ide_selection>`.** That is
  the editor injecting context, not something the person typed. Do not count it
  against them — but do say if the volume was enough to matter.
- **One session in the logs may be this review itself.** Ignore it.
