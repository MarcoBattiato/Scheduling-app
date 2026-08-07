# How to run the workflow review

**This file is for you.** The other session reads `ANALYSIS_PROMPT.md`; you do
not need to read that one.

---

## 1. Build the data

In a terminal, from the project root:

```bash
python3 .claude/workflow_review/extract.py --all --format csv \
    > .claude/workflow_review/turns.csv
```

This reads the session logs Claude Code already keeps and writes one row per
prompt. Takes a second. Re-run it whenever you want the review to include
newer work.

## 2. Open a new Claude Code session

In the **same folder** (`Scheduling-app`). A new session, not this one — the
point is a reviewer with no stake in the work.

## 3. Give it one line

```
Read .claude/workflow_review/ANALYSIS_PROMPT.md and carry it out.
```

That is the whole prompt. Everything it needs — what to look at, what to look
for, and what to be careful of — is in that file.

## 4. Read what comes back

It will lead with the three or four findings worth acting on, each with a
rewrite of one of your actual prompts. Push back if a finding seems wrong; it
is working from a table and a sample, not from memory.

---

## If you want to look yourself first

One line per prompt, with timings and token counts:

```bash
python3 .claude/workflow_review/extract.py --all
```

Any single exchange in full — the prompt, the reply, every tool call:

```bash
python3 .claude/workflow_review/extract.py --all --turn 26
```

## Notes

- `turns.csv` is not committed. It is rebuilt by step 1 and contains every
  prompt verbatim.
- The review session's own log lands in the same place, so a later run will
  show it reviewing itself. Harmless.
- `README.md` in this folder explains why the review works this way. You do not
  need it to run one.
