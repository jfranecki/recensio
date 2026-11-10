# recensio — runbook for sessions started in this folder

This folder is the home of **recensio**, a **dreaming** tool: an out-of-band pass over
Claude Code's memory stores on this machine. It reads session transcripts, finds patterns across sessions, and
**proposes** memory changes that the user accepts or rejects. `README.md` explains it for
people; this file is the runbook for you.

Sessions started here exist to run three commands.

## The three commands

| Command | What it does | Cost | Writes to memory stores? |
|---|---|---|---|
| `/digest [scope]` | One worker per transcript chunk writes a digest; `dreamctl` checks it, stamps the watermark and files it as `digests/<store>/<session>.md`. Only sessions without a current digest are read. Runs in batches. | cheap after the first run | no |
| `/dream [scope]` | Digests first, then one worker per store reads the store and all its digests and proposes changes; an optional global pass proposes rules that hold in every project. Writes `reports/<date>/`. | the expensive step; run monthly or on demand | no |
| `/review [report]` | Opens `review.html`, records accept/reject per proposal, then applies only accepted ones with a snapshot first. | trivial | **yes, only here** |

Scope for `/digest` is `all` (default) or one store name as listed by
`python tools/dreamctl.py projects`. Scope for `/dream` is `all`, `global`, one store
name, or a comma-separated list of store names with an optional `global`.

Each command is a short sequence of `python tools/dreamctl.py ...` calls that print JSON,
plus one Workflow call. The `prepare` steps write a brief file per worker (every path the
worker needs, absolute) and a small `workflow-args.json`; pass that object to the Workflow
tool as `args`. Session ids and long paths are never retyped by the main session.

## Stores

- **Project stores:** `~/.claude/projects/<munged-path>/memory/` with a `MEMORY.md` index
  and one file per fact. Native file shape: frontmatter `name`, `description`,
  `metadata.type`, then the fact with **Why** and **How to apply**.
- **Global store** (optional, see `global_store` in `config.json`): a folder of memory
  files, indexed by `@` import lines under a heading in the user-level `CLAUDE.md`. Native
  shape: Context / Trigger, Core Rule / Insight, Expected Outcome. Only `feedback` and
  `user` types, and only rules that are correct in every session on this machine.
- Skipped unless named explicitly: this folder's own store (its sessions are the dreaming
  runs) and anything matching `exclude_name_patterns` in `config.json`.

## Layout

```
config.json          paths, exclusions, chunk size, batch size, worker models, digest_version
tools/dreamctl.py    the mechanical half: projects, pending, digest-prepare, digest-finish,
                     dream-prepare, assemble, report, render, serve, decide, apply
tools/selftest.py    runs the whole pipeline in a scratch folder with fake stores; no model
tools/review_template.html
prompts/             digest.md, dream-project.md, dream-global.md (what the workers read)
workflows/           digest.js, dream.js (Workflow tool scripts; invoked with scriptPath + args)
digests/<store>/<session>.md   the watermark is the frontmatter (source_size, digest_version)
runs/<ts>-digest/    scratch: extract/ chunks, jobs/ briefs, out/ staged digests, manifest
runs/<ts>-dream/     scratch: packets/, jobs/ briefs, proposals-<store>.json, manifest
reports/<date>/      proposals.json, REPORT.md, review.html, decisions.json, applied.json
snapshots/<stamp>/<store>/   copy of each store taken before apply (rollback = copy back)
archive/<stamp>/<store>/     soft-deleted memory files with a .why.md each
state.json           last digest / last dream timestamps
docs/media/          the animated clips in README.md
docs/demo/           the scripts that generate those clips from a made-up project; dev-only,
                     not part of the three commands, and never to be pointed at real data
```

`digests/`, `reports/`, `snapshots/`, `archive/`, `runs/` and `state.json` are the user's
private data and are git-ignored in a default clone.

## Rules

1. **Proposals only.** Only `dreamctl.py apply`, run by `/review` after decisions are
   recorded, writes to a memory store. Never edit a memory store by hand from here, and
   never let a worker do it.
2. **Workers write their own outputs.** Digests and proposal files are written by the
   subagents directly, into the run folder. Do not route their content through the main
   context. A digest reaches `digests/` only through `digest-finish`, which checks its
   shape and writes the frontmatter itself; a worker never writes the watermark.
3. **Model tiers come from `config.json`.** `workers.digest`, `workers.dream` and
   `workers.global` say which model or agent definition does each role. `dreamctl` passes
   them to the workflow; do not override them in the Workflow call.
4. **Read-only verification is allowed.** Dreamers may run `git log`, `git show`, `ls`,
   `test -e` inside project directories to check whether a memory is stale. Nothing that
   writes.
5. **Workflow opt-in.** The `/digest` and `/dream` commands are the user's standing
   opt-in for the Workflow tool. Use `scriptPath` with the shipped scripts; do not
   rewrite them inline. Without the Workflow tool, the commands say how to do the same
   with the Agent tool.
6. **Watermarks are the digest files.** A session is pending when no digest exists, the
   transcript grew past `source_size`, or `digest_version` in `config.json` was raised.
   Raising `digest_version` re-digests everything on the next `/digest`. Raise it whenever
   the extractor in `dreamctl.py` or `prompts/digest.md` changes what a digest captures.
   A transcript written to in the last `active_session_minutes` (10) is treated as a
   session still open and waits for a later `/digest`, so a live session is not digested
   over and over.
7. **Commit only when asked to by the tool.** When `dreamctl` output carries a `commit`
   list or a `to commit:` line, the user tracks their data folders in git: run those
   commands. Otherwise the data folders are ignored and there is nothing to commit. Never
   push.
8. **Decisions are the user's.** Never record an accept or a reject the user did not
   give, and never run `apply` against a real store outside `/review`.
9. **Test in a scratch folder.** `python tools/selftest.py` after any change to
   `dreamctl.py`. To try something by hand, `python tools/dreamctl.py --root <scratch> ...`
   keeps digests, runs, reports, snapshots and archive out of this checkout, and
   `apply --remap STORE=<copy> --user-claude-md <copy>` keeps writes off the real stores.

## Worth keeping in mind

- `apply` refuses to run while a memory folder holds a `.consolidate-lock` file and
  hash-checks every target file, so it cannot collide with another process that is
  consolidating the same store. If it refuses, retry later.
- Claude Code deletes transcripts after `cleanupPeriodDays` (user `settings.json`; 30 days
  unless raised). Digests kept here outlive them.
- The transcript JSONL format is not a documented interface. If a Claude Code update
  changes it and user turns stop appearing in `runs/<ts>-digest/extract/**/chunk-*.txt`,
  fix `extract_session` in `tools/dreamctl.py`, extend `tools/selftest.py`, and raise
  `digest_version`.
