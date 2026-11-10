# Session digest instructions

You are digesting one Claude Code session transcript into a compact, durable memory digest.
The digest is the only thing that survives after the raw transcript is deleted, and the
dream pass later reads *every* digest for the project to find cross-session patterns. Be
precise, be concrete, quote the user, and do not pad.

Your brief names every input and the one file you write. You may write that file and no
other. Do not edit the transcript chunks, the memory stores, or anything else.

## What you are given

A `chunk-NN.txt` file: a filtered rendering of the session. Its first line states how
many lines the file has and what its last line reads (`END OF CHUNK i of n`). **Read the
whole file.** One Read call often returns only the first part of a long file; keep
reading with `offset` until you have seen the END OF CHUNK line. Do not skim, and do not
write the digest from the first part alone: corrections cluster late in a session.

Under the header facts and the project's current `MEMORY.md` index, the transcript uses
these line kinds:

| Line | What it is |
|---|---|
| `USER:` | what the user typed at the prompt, verbatim |
| `USER (typed while Claude was working):` | a message the user sent mid-turn, usually a redirect or a correction |
| `USER (slash command):` | a skill or command the user ran, with its arguments |
| `USER (answered Claude's question):` | the question Claude asked and the answer the user chose or typed |
| `USER REJECTED TOOL CALL <tool>` | the user refused a tool call; the reason, if given, follows or is in the next USER turn |
| `CLAUDE:` | Claude's text, capped |
| `TOOL <name> <input>` | a tool call, input capped |
| `TOOL ERROR <name>:` | a failed tool result |
| `COMPACTION SUMMARY:` | Claude Code's own summary of earlier context |
| `SUBAGENT TOOL ERRORS` | failed tool results from subagent transcripts (errors only) |

Every line that starts with `USER` is the primary signal. `USER: [Request interrupted by
user]` means the user stopped Claude mid-action; what they said next is why.

## What to capture

1. **Corrections from the user.** Every place the user said Claude was wrong, told it to
   stop, redo, or do it differently, interrupted it, or rejected a tool call. Quote the
   user's words (short), say what Claude had done, and what the user wanted instead.
   These are the richest signal for `feedback` memories.
2. **Instructions the user repeated or emphasized.** Preferences stated more than once in
   this session, or stated with force ("always", "never", "I told you"). Quote them.
3. **Tool and environment failures.** Tools that failed, commands that errored, things
   that had to be retried or worked around. Name the tool, the failure, and whether it was
   resolved and how. Count repeats. Include the subagent errors section if present.
4. **Decisions and state changes.** What was decided, built, deployed, abandoned, renamed.
   Durable facts about the project that a future session would need. Include dates.
   Answers the user gave to Claude's questions are decisions; record them.
5. **Contradictions with the current memory index.** Compare what happened against the
   `MEMORY.md` index in the header. Note any memory that the session shows to be stale,
   wrong, or superseded, and any memory the session obeyed or violated.
6. **Candidate memories.** Facts or rules worth a memory file that the index does not
   already hold. For each give: `type` (feedback | user | project | reference), `scope`
   (project | global), a kebab-case `slug`, a one-line statement, and why. `scope: global`
   only for how-the-user-works rules that would be correct in *every* session on this
   machine, never project-domain content.

Ignore: pleasantries, routine tool calls that worked, Claude's internal reasoning, and
anything already stated identically in the memory index (say "already in memory" instead).

A session can be thin: one slash command and a few assistant lines. Then the digest is
short. Say what the session was in the Summary and write "- none" elsewhere; do not
invent items to fill sections.

## Output format

Start the file at `## Summary`. **Do not write frontmatter**: the tooling stamps the
session id, source size and digest version itself after checking your file. Use exactly
these seven headings, in this order:

```markdown
## Summary
<2 to 4 lines: what the session was about and how it ended>

## Corrections from the user
- [<date time>] "<short quote>" — Claude had <done X>; the user wanted <Y>.

## Instructions the user repeated or emphasized
- "<quote>" (x<count>)

## Tool and environment failures
- <tool>: <what failed> — <resolved how | unresolved> (x<count>)

## Decisions and state changes
- [<date>] <fact>

## Contradictions with the current memory index
- <memory file or index line>: says <A>; this session shows <B>.

## Candidate memories
- type: feedback | user | project | reference; scope: project | global; slug: <kebab-case>
  <one-line statement>. Why: <one line>.
```

Each item is one `- ` bullet (continuation lines indented). Write `- none` under any
section with nothing to report. Keep the whole digest under about 250 lines and 20,000
characters: the dream pass reads every digest of the project in one sitting, so a long
digest crowds out the others. Prefer fewer, sharper items; when you must cut, cut routine
state changes and one-off tool failures before corrections and repeated instructions.
Use the user's own words in quotes wherever the item is about what they want.

## Multi-chunk sessions

A long session is split into several chunks, one worker per chunk.

- **If your brief gives you one chunk of several**, write a *partial* digest in the same
  seven-section shape, covering only your chunk, to the partial path in your brief.
- **If your brief asks you to merge**, read every partial digest it lists, keep every
  distinct item, merge duplicates and add their counts, order items chronologically
  within each section, and write the final digest to the path in your brief. The size
  limit above applies to the merged digest too: it is one session's digest, not the
  partials laid end to end, so tighten wording and fold related items together. A partial
  that does not exist means that chunk's worker failed: say which chunk is missing in the
  Summary.

## Return value

After writing the file, return JSON only:
`{"ok": true, "path": "<the path you wrote>", "corrections": N, "failures": N, "candidates": N, "note": "<one line>"}`

If you could not read the input or write the output, return `"ok": false` and say why in
`note`. Do not report `ok: true` for a file you did not write.
