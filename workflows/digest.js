export const meta = {
  name: 'dreaming-digest',
  description: 'Digest pending Claude Code session transcripts into per-session memory digests',
  phases: [
    { title: 'Digest', detail: 'one worker per transcript chunk' },
    { title: 'Merge', detail: 'combine partial digests of long sessions' },
  ],
}

// args = the object in runs/<ts>-digest/workflow-args.json, written by `dreamctl digest-prepare`:
//   { run: "<absolute run dir, forward slashes>",
//     worker: { model: "sonnet" } | { agentType: "<agent name>" } | {},   // from config.json `workers.digest`
//     jobs: [["<label>", <chunk count>], ...] }
// Every path a worker needs is in its brief (runs/<ts>-digest/jobs/NNN*.md), also written by
// dreamctl, so nothing here depends on a session id or a path being retyped correctly.
// Briefs are numbered by position in `jobs`, starting at 001.
const m = args
if (!m || typeof m.run !== 'string' || !Array.isArray(m.jobs)) {
  throw new Error('args must be the object in workflow-args.json: { run, worker, jobs: [[label, chunks], ...] }')
}
const WORKER = m.worker || {}   // {} = the session model

const SUMMARY = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    path: { type: 'string' },
    corrections: { type: 'number' },
    failures: { type: 'number' },
    candidates: { type: 'number' },
    note: { type: 'string' },
  },
  required: ['ok', 'path', 'note'],
}

const pad = (n, w) => String(n).padStart(w, '0')
const brief = (i, suffix) => `${m.run}/jobs/${pad(i + 1, 3)}${suffix}.md`
const ask = (path) => `Digest job. Your brief is this file: ${path}

Read the brief and do exactly what it says. It names the instructions, the input to read in full, and the one file you may write. Write that file and no other. Return only the JSON summary the instructions describe.`

const worker = (path, label, phase) =>
  agent(ask(path), { ...WORKER, label, phase, schema: SUMMARY })

const results = await pipeline(m.jobs, async (job, _item, i) => {
  const [name, n] = job
  const label = `digest:${name}`
  if (n === 1) {
    const r = await worker(brief(i, ''), label, 'Digest')
    return { label, ok: !!(r && r.ok), note: r ? r.note : 'worker died' }
  }
  const parts = await parallel(
    Array.from({ length: n }, (_, k) => () => worker(brief(i, `-c${pad(k + 1, 2)}`), `${label}#${k + 1}/${n}`, 'Digest')),
  )
  const good = parts.filter(Boolean).filter((p) => p.ok).length
  if (!good) {
    log(`${label}: every chunk failed; no digest written`)
    return { label, ok: false, note: 'all chunks failed' }
  }
  if (good < n) log(`${label}: ${n - good} of ${n} chunks failed; merging the rest`)
  const r = await worker(brief(i, '-merge'), `${label}:merge`, 'Merge')
  return { label, ok: !!(r && r.ok), note: r ? r.note : 'merge worker died', chunksMerged: good, chunksTotal: n }
})

const done = results.map((r, i) => r || { label: `digest:${m.jobs[i][0]}`, ok: false, note: 'stage threw' })
const failed = done.filter((r) => !r.ok)
log(`workers reported ${done.length - failed.length} of ${done.length} sessions digested${failed.length ? `; ${failed.length} failed` : ''}. Run digest-finish: it checks the files and is the source of truth.`)
return { run: m.run, sessions: done.length, reportedOk: done.length - failed.length, failed }
