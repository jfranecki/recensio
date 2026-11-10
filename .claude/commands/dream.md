---
description: Dream over the memory stores — digest new sessions, then propose verified, organized, enriched memory changes per project and globally. Proposals only; nothing is applied until /review.
argument-hint: "[all | global | <store name>[,<store name>...][,global]] [--no-digest]"
---

# /dream — propose memory changes

Arguments: `$ARGUMENTS`. The first word is the scope (`all` when empty); `--no-digest`
skips step 1. Below, `<scope>` stands for that value; type it into the commands yourself.
Work from the Dreaming root. Read `CLAUDE.md` here if you have not already.

Scope forms: `all` (every project store, then the global pass), `global` (the global pass
alone), one store name, or a comma-separated list of store names with an optional
`global` at the end. Store names are the ones `python tools/dreamctl.py projects` lists.

Output is a report under `reports/<date>/` with `proposals.json`, `REPORT.md` and
`review.html`. **Nothing is written to any memory store by this command.**

## 1. Digest first

Run the steps of `/digest` (see `.claude/commands/digest.md`) for the same stores. For
`all` and `global`, digest `all`: the global pass reads every project's digests. Skip when
`--no-digest` was passed or nothing is pending.

## 2. Prepare packets and briefs

```
python tools/dreamctl.py dream-prepare --scope=<scope>
```

It prints JSON: `run`, `agents`, one entry per store (memory files, digests, packet size),
`large_packets`, `workflow_script` and `workflow_args_file`. Tell the user how many stores
and agents this run takes. If `large_packets` is not empty, name those stores: their
dreamers read more than `packet_warn_chars` characters and are the likeliest to fail.

## 3. Run the dream workflow

Read `workflow_args_file` (the run path, the `workers` setting, one label per project
store, and whether the global pass runs). Call the Workflow tool with `scriptPath` =
`workflow_script` and `args` = that object (the parsed JSON value, not a string). This
command is the explicit opt-in: one worker per store writes
`<run>/proposals-<store>.json`; then the global pass writes `proposals-global.json`. Each
worker reads its own brief under `<run>/jobs/`. Wait for the completion notification; do
not poll.

**If the Workflow tool is unavailable in this session**, use the Agent tool instead: one
agent per `<run>/jobs/dream-NNN.md` brief with the prompt "Dream job. Your brief is this
file: <path>. Read the brief and do exactly what it says. Write the one file it names and
no other." Take the agent from `workers.dream` in the args file: its `agentType` as the
subagent type, or its `model` as the model; an empty object means the default agent. When
they are all done, run the global pass the same way from `<run>/jobs/dream-global.md`
with `workers.global`, or follow that brief yourself when `workers.global` is empty.

## 4. Assemble the report

```
python tools/dreamctl.py assemble --run <run>
```

It validates every proposal, attaches file hashes, writes `reports/<date>/` and renders
`review.html`, then prints a JSON summary: the report path, counts per store and action,
the high-confidence titles, and `problems` (dropped proposals and stores whose dreamer
wrote nothing). Give the user those counts, the high-confidence titles, and the problems
if any.

If the JSON has a `commit` list, this checkout tracks its reports in git: run the two
commands in it. If `commit` is null, skip this.

End by telling the user to run `/review` when they want to go through the proposals. Do
not apply anything, and do not record decisions on their behalf.
