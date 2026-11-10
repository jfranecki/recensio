---
description: Digest every session transcript that has no current digest (the map step of dreaming). Cheap, idempotent, safe to run often.
argument-hint: "[all | <store name as listed by `dreamctl.py projects`>]"
---

# /digest — turn new session transcripts into digests

Scope: `$ARGUMENTS`. An empty scope means `all`. Below, `<scope>` stands for that value;
type it into the commands yourself. Work from the Dreaming root (this folder). Read
`CLAUDE.md` here if you have not already.

This step reads raw transcripts under `~/.claude/projects` and writes one digest per
session under `digests/<store>/<session>.md`. It never touches any memory store.

Every step is one `python tools/dreamctl.py ...` call that prints JSON (use `python3` if
that is what this machine calls it). Nothing depends on shell variables surviving between
calls, and the calls work the same in Bash and PowerShell.

## 1. Prepare one batch

```
python tools/dreamctl.py digest-prepare --project=<scope>
```

If the JSON says `"pending_sessions": 0`, say so and stop. `skipped_still_active` lists
sessions whose transcript was written to in the last few minutes: they are probably still
open, so they wait for a later `/digest` (mention them; do not try to force them).
Otherwise tell the user how many sessions and MB are pending in total and how many this
batch takes. A batch is capped
at `digest_batch_agents` workers (`config.json`, 45 by default), so a large backlog takes
several batches; a first run over a few months of sessions can take an hour or more. Say
that plainly and continue.

The JSON gives `run`, `workflow_script`, `workflow_args_file` and `batch_agents`. If
`batch_agents` is 0 the batch held only empty sessions: skip step 2.

## 2. Run the digest workflow

Read `workflow_args_file` (a small JSON object: the run path, the `worker` setting, and
one `[label, chunks]` pair per session). Call the Workflow tool with `scriptPath` =
`workflow_script` and `args` = that object (the parsed JSON value, not a string). This
command is the explicit opt-in for multi-agent orchestration: one worker per transcript
chunk, plus a merge worker for each long session. Each worker reads its own brief under
`<run>/jobs/`. Wait for the completion notification; do not poll.

**If the Workflow tool is unavailable in this session**, use the Agent tool instead: one
agent per brief file in `<run>/jobs/`, with the prompt "Digest job. Your brief is this
file: <path>. Read the brief and do exactly what it says. Write the one file it names and
no other." Take the agent from the `worker` object in the args file: its `agentType` as
the subagent type, or its `model` as the model; an empty object means the default agent.
Run up to six at a time. For a session with `-cNN` briefs, run those first and its
`-merge` brief after they finish.

## 3. Finish the batch

```
python tools/dreamctl.py digest-finish --run <run>
```

This is the source of truth, not what the workers reported. It checks each staged digest,
stamps the watermark frontmatter from the extraction facts, and moves it into `digests/`.
`missing` and `malformed` list sessions whose worker failed; they stay pending.

If the JSON has a `commit` list, this checkout tracks its digests in git: run the two
commands in it. If `commit` is null, the data folders are git-ignored here; skip this.

## 4. Repeat until nothing is pending

If `still_pending_in_scope` is above 0, go back to step 1 for the next batch. Stop early
if a batch seals nothing (every worker failed): report the failures instead of looping.

When done, report: sessions digested, the `totals` (corrections, failures, candidate
memories) summed over the batches, and any sessions still missing or malformed. Say that
running `/digest` again retries only those. Do not paste digests into the chat.
