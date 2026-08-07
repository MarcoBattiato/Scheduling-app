# Prompt for the review session

Paste this into a fresh Claude Code session (a different one from the work
being reviewed, so it has no stake in defending it).

---

I want an honest review of how I have been working with you on this project,
aimed at making my prompts and workflow better. Be critical. I am not looking
for reassurance, and findings that make me look inefficient are the useful ones.

**The material.** Run this to get one row per prompt, with timestamps,
latency, tool calls, files touched, and exact token counts:

```
python3 .claude/workflow_review/extract.py --all --format csv > /tmp/turns.csv
```

The raw transcripts, if you need the actual exchanges, are in
`~/.claude/projects/<project-slug>/*.jsonl`. Read the specific turns you want
to quote rather than the whole file — they are large.

The git log is a second, independent record: this project's commit messages
carry the reasoning behind each decision, so `git log` shows what was actually
concluded, while the transcript shows what it cost to get there.

**What I want to know.**

1. **Which of my prompts caused rework.** Find places where something was built,
   then changed or undone a few turns later because my instruction was
   ambiguous, incomplete, or contradicted something earlier. Quote both prompts
   and say what wording would have avoided it. This is the most valuable output;
   spend most of your effort here.

2. **Where the tokens actually went.** Which turns were expensive, and was the
   spend justified by what came out? Distinguish expensive-and-productive from
   expensive-and-wasted. Look for: repeated reading of the same files, long
   tool chains that ended in a small change, work that was later discarded.

3. **Decisions I changed my mind about**, and whether the reversal was caused by
   new information (fine, unavoidable) or by not having thought it through
   before asking (worth changing). Say which.

4. **Where I gave too little context, and where too much.** Both cost. Point at
   specific prompts.

5. **Tools and features I did not use but should have.** Consider: plan mode,
   subagents for parallel investigation, `/code-review`, custom slash commands
   for repeated instructions, hooks for things I asked for repeatedly by hand,
   `CLAUDE.md` for standing preferences I kept restating, and memory for facts I
   repeated. Only name ones that would genuinely have helped *this* work, and
   say which turn they would have helped at.

6. **Anything I repeated more than twice.** Repetition is the clearest signal
   that something belongs in configuration rather than in a prompt.

**How to answer.** Lead with the three or four findings that would save the most
time or tokens, each with a concrete before/after rewrite of one of my actual
prompts. Then the supporting detail. Keep the whole thing short enough to act
on — a long report I skim is worse than a short one I use.

**Cautions.**
- Latency in the table is wall-clock between my prompt and the last reply
  record. It includes my own reading and typing time when I interrupted, so
  treat large values as suspect rather than as thinking time.
- Token counts are raw. Cache reads are much cheaper than cache writes or
  output; do not add the four columns together and call it cost. Look up
  current pricing if you want money figures, rather than assuming a rate.
- Some turns are IDE noise (`<ide_opened_file>`, `<ide_selection>`) rather than
  things I typed. Do not count those against me, but do note if they were
  numerous enough to matter.
