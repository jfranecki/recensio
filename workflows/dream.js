export const meta = {
  name: 'dreaming-dream',
  description: 'Propose memory changes per project store, then one global pass over every nomination',
  phases: [
    { title: 'Project dreams', detail: 'one worker per memory store: verify, organize, enrich' },
    { title: 'Global dream', detail: 'reads every nomination and proposes global changes' },
  ],
}

// args = the object in runs/<ts>-dream/workflow-args.json, written by `dreamctl dream-prepare`:
//   { run: "<absolute run dir, forward slashes>",
//     workers: { dream: <opts>, global: <opts> },   // from config.json `workers`
//     projects: ["<label>", ...], global: true|false }
// <opts> is { model: "opus" }, { agentType: "<agent name>" }, or {} for the session model.
// Every path a worker needs is in its brief (runs/<ts>-dream/jobs/dream-NNN.md and
// dream-global.md), also written by dreamctl. Briefs are numbered by position in `projects`.
const m = args
if (!m || typeof m.run !== 'string' || !Array.isArray(m.projects)) {
  throw new Error('args must be the object in workflow-args.json: { run, workers, projects: [label, ...], global }')
}
const WORKERS = m.workers || {}

const OUT = {
  type: 'object',
  properties: {
    store: { type: 'string' },
    proposals: { type: 'number' },
    path: { type: 'string' },
    note: { type: 'string' },
  },
  required: ['store', 'proposals', 'path'],
}

const pad = (n, w) => String(n).padStart(w, '0')
const ask = (path, what) => `Dream job (${what}). Your brief is this file: ${path}

Read the brief and do exactly what it says. It names the instructions, the packet to read in full, and the one file you may write. Write that file and no other; never write to a memory store or a project directory. Return only the JSON summary the instructions describe.`

// Barrier is deliberate: the global pass reads every per-project proposals file.
const per = await parallel(
  m.projects.map((label, i) => () =>
    agent(ask(`${m.run}/jobs/dream-${pad(i + 1, 3)}.md`, 'one project memory store'), {
      ...(WORKERS.dream || {}), label: `dream:${label}`, phase: 'Project dreams', schema: OUT,
    })),
)
const failed = m.projects.filter((_, i) => !per[i])
const total = per.filter(Boolean).reduce((a, r) => a + (r.proposals || 0), 0)
log(`project dreams: ${m.projects.length - failed.length} of ${m.projects.length} stores done, ${total} proposals${failed.length ? `; failed: ${failed.join(', ')}` : ''}`)

let global = null
if (m.global) {
  // With no `workers.global` setting this agent runs at the session model.
  global = await agent(ask(`${m.run}/jobs/dream-global.md`, 'the global memory store'), {
    ...(WORKERS.global || {}), label: 'dream:global', phase: 'Global dream', schema: OUT,
  })
  log(`global dream: ${global ? global.proposals + ' proposals' : 'failed'}`)
}

return { run: m.run, projects: per.filter(Boolean), failedProjects: failed, global }
