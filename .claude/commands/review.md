---
description: Review the latest dream report in the browser (or in the terminal with --cli), then apply only what was accepted, with a snapshot of every touched store first.
argument-hint: "[reports/<date> | latest] [--cli]"
---

# /review — accept or reject proposals, then apply

Arguments: `$ARGUMENTS`. The first word that is not `--cli` is the report (`latest` when
absent). Below, `<report>` stands for that value; type it into the commands yourself. Work
from the Dreaming root. Read `CLAUDE.md` here if needed.

## 1. Pick the report

```
python tools/dreamctl.py report <report>
```

`latest` means the newest report under `reports/` that has no `applied.json`. The JSON
gives the report path, the proposal counts per store, the decisions already recorded and
whether the report was applied. Tell the user which report it is and how many proposals it
holds. If it says `"applied": true`, say so and stop.

Use the printed `report` path as `<report>` from here on, so every later step works on the
same report even if a new one appears.

## 2. Collect decisions

Decisions are the user's. Never record an accept or a reject they did not give.

**Browser (default).** Run the review server in the background and wait for its task
notification; do not poll:

```
python tools/dreamctl.py serve <report>
```

It opens `review.html` in the default browser, saves every click to
`<report>/decisions.json` immediately, and exits with code 0 when the user clicks Finish,
or code 3 after three idle hours. On code 3 tell the user their decisions so far are saved
and `/review` resumes where they left off; stop there.

**Terminal (`--cli`).** For each proposal in `<report>/proposals.json`, ask with
AskUserQuestion (up to four proposals per call): question = title, store/file, action,
summary; options Accept / Reject / Skip. Record each answer:

```
python tools/dreamctl.py decide <report> --id p001 --decision accept
```

After the last one, add `--finish` to the final `decide` call.

## 3. Apply

```
python tools/dreamctl.py apply <report> --dry-run
python tools/dreamctl.py apply <report>
```

Show the dry-run plan first, then apply. The apply step:
- refuses to run if a store holds a `.consolidate-lock` (Claude Code's own auto-dream is running);
- checks every accepted proposal against the stores before the first write, and skips any
  file whose content changed since the dream saw it (hash guard), saying so;
- copies every touched store to `snapshots/<stamp>/<store>/` before writing;
- writes files, then makes each `MEMORY.md` agree with what was accepted (one index line
  per file that exists, none for files that do not), and adds `@` import lines to the user
  `CLAUDE.md` for global adds;
- moves deleted files to `archive/<stamp>/<store>/` with a `.why.md` beside each;
- prints `warning` lines for index entries without a file and files without an index entry.

If the apply output ends with a `to commit:` line, this checkout tracks its reports and
snapshots in git: run the commands on that line. Otherwise skip this.

Report what was applied, what was skipped and why, any warnings, and the snapshot stamp to
copy back if anything needs to be undone (`snapshots/<stamp>/<store>/` holds the store as
it was). Remind the user that memory changes load in new sessions, not the current one.
