#!/usr/bin/env python3
"""The made-up world behind the README clips. Dev-only; the tool itself never imports this.

Builds a throwaway home folder with two fake projects, five fake Claude Code transcripts, a
small memory store and a global store, then runs the *real* pipeline over it:

    digest-prepare -> digest-finish -> dream-prepare -> assemble -> (review) -> apply

Only the two model steps are faked: the digests and the proposals a worker would write are
written here by hand. Everything dreamctl prints is captured for the clips, so the numbers
and messages they show are what the tool really said about this demo data.

Nothing here reads your own ~/.claude: every dreamctl call runs with HOME pointed at the
throwaway folder.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DREAMCTL = REPO / "tools" / "dreamctl.py"

ACME = "-home-sam-acme-api"      # the store name Claude Code gives /home/sam/acme-api
NOTES = "-home-sam-notes-site"
SESSIONS = [  # store, session id, day in November 2026, transcript size in MB
    (ACME, "3f2a91c0-5d1e-4b7a-9c2f-0a1b2c3d4e5f", 2, 12.4),
    (ACME, "8b1d44e2-7a90-4c1d-8e3b-1f2a3b4c5d6e", 4, 9.8),
    (ACME, "c07e5a13-2b6f-4d8e-a1c4-9e8d7c6b5a4f", 6, 7.1),
    (ACME, "e94b02fd-6c3a-4f1b-b7d2-3a4b5c6d7e8f", 8, 8.3),
    (NOTES, "51aa7c66-9e0d-4a2c-8f1e-6b7c8d9e0f1a", 9, 3.7),
]

# What was said in each session. kinds: user, queued (typed while Claude was working),
# claude, tool (name, input), reject (tool, input, what the user said when refusing it).
TURNS = {
    "3f2a91c0": [
        ("user", "Add a /health endpoint that reports the build version."),
        ("claude", "I'll add the route and a test for it."),
        ("tool", "Edit", {"file_path": "app/routes/health.py"}),
        ("tool", "Bash", {"command": "git commit -am \"add health endpoint\""}),
        ("queued", "Run the tests before you commit. Always."),
        ("claude", "You're right. I committed before running the suite. Running it now."),
        ("tool", "Bash", {"command": "pytest -q"}),
        ("claude", "All 212 tests pass. The commit stands; /health returns the build version."),
    ],
    "8b1d44e2": [
        ("user", "Ship 1.4.0 to production."),
        ("claude", "My notes say to run make deploy-staging and then promote by hand from the dashboard. Starting with staging."),
        ("queued", "Stop. We don't deploy by hand any more. CI deploys when you push a tag."),
        ("claude", "Understood. Tagging v1.4.0 and pushing the tag instead."),
        ("tool", "Bash", {"command": "git tag v1.4.0 && git push origin v1.4.0"}),
        ("claude", "The pipeline deployed to staging, ran the smoke tests and promoted to production."),
    ],
    "c07e5a13": [
        ("user", "Rename the orders table column qty to quantity, with a migration."),
        ("tool", "Edit", {"file_path": "migrations/0042_rename_qty.py"}),
        ("tool", "Bash", {"command": "git commit -am \"rename qty to quantity\""}),
        ("user", "Did you run the tests first? Run them before every commit."),
        ("tool", "Bash", {"command": "pytest -q"}),
        ("claude", "Two tests failed on the old column name. Fixed and committed again."),
        ("user", "Also, we moved to Node 20 last month. You can stop pinning Node 16 in the build."),
        ("claude", "Removed the pin from the build script."),
    ],
    "e94b02fd": [
        ("user", "Clean up the build output and add request logging."),
        ("reject", "Bash", {"command": "rm -rf build/ dist/"}, "Ask me before you delete anything."),
        ("claude", "Sorry. I'll leave build/ and dist/ alone and only add the logging."),
        ("tool", "Edit", {"file_path": "app/middleware/logging.py"}),
        ("tool", "Bash", {"command": "git commit -am \"add request logging\""}),
        ("queued", "Tests first, then commit. I keep having to say this."),
        ("tool", "Bash", {"command": "pytest -q"}),
        ("claude", "Tests pass."),
    ],
    "51aa7c66": [
        ("user", "Regenerate the site and tidy the output folder."),
        ("claude", "I'd like to clear public/ before rebuilding. Shall I?"),
        ("user", "Yes, but ask before running anything destructive, like you just did. Always."),
        ("tool", "Bash", {"command": "rm -rf public/ && make site"}),
        ("claude", "Site rebuilt: 48 pages."),
    ],
}

# Stand-ins for what a digest worker writes (the seven sections of prompts/digest.md).
DIGESTS = {
    "3f2a91c0": {
        "Summary": ["Added a /health endpoint that reports the build version. Claude committed before running the tests and was corrected mid-task; the suite then passed."],
        "Corrections from the user": ['[2026-11-02 14:10] "Run the tests before you commit. Always." — Claude had committed untested code; the user wanted the suite run first.'],
        "Decisions and state changes": ["[2026-11-02] /health reports the build version."],
        "Candidate memories": ["type: feedback; scope: project; slug: run-tests-first\n  Run the test suite before every commit. Why: stated with \"Always\" after an untested commit."],
    },
    "8b1d44e2": {
        "Summary": ["Released 1.4.0. Claude began the manual staging deploy from memory and was stopped: releases now go through CI on a tag push."],
        "Corrections from the user": ['[2026-11-04 10:32] "Stop. We don\'t deploy by hand any more. CI deploys when you push a tag." — Claude had started make deploy-staging; the user wanted a tag pushed.'],
        "Decisions and state changes": ["[2026-11-04] v1.4.0 tagged and pushed; CI deployed to staging, ran smoke tests, promoted to production."],
        "Contradictions with the current memory index": ["deploy-steps.md: says promote to production by hand; this session shows CI deploys on a tag push."],
        "Candidate memories": ["type: project; scope: project; slug: deploy-steps\n  CI deploys when a version tag is pushed. Why: the user said hand deploys are over."],
    },
    "c07e5a13": {
        "Summary": ["Renamed orders.qty to quantity with a migration. Claude again committed before running the tests. The build no longer pins Node 16."],
        "Corrections from the user": ['[2026-11-06 16:05] "Did you run the tests first? Run them before every commit." — Claude had committed the migration untested; two tests then failed.'],
        "Instructions the user repeated or emphasized": ['"Run them before every commit." (x1; also said on 2 Nov)'],
        "Tool and environment failures": ["Bash: pytest failed on the old column name — fixed and re-run (x1)"],
        "Decisions and state changes": ["[2026-11-06] The project moved to Node 20; the Node 16 pin was removed from the build script."],
        "Contradictions with the current memory index": ["node-16-pin.md: says keep Node pinned to 16; the user says the project moved to Node 20 last month."],
        "Candidate memories": ["type: feedback; scope: project; slug: run-tests-first\n  Run the tests before every commit. Why: second session with the same correction."],
    },
    "e94b02fd": {
        "Summary": ["Added request logging. The user refused a command that would have deleted build/ and dist/, and corrected commit-before-test for the third time."],
        "Corrections from the user": ['[2026-11-08 09:48] "Ask me before you delete anything." — Claude had tried to run rm -rf build/ dist/; the user rejected the call.',
                                      '[2026-11-08 10:02] "Tests first, then commit. I keep having to say this." — Claude had committed the logging change untested.'],
        "Instructions the user repeated or emphasized": ['"Tests first, then commit." (third session)'],
        "Decisions and state changes": ["[2026-11-08] Request logging middleware added."],
        "Candidate memories": ["type: feedback; scope: project; slug: run-tests-first\n  Tests before commit. Why: \"I keep having to say this.\"",
                               "type: feedback; scope: global; slug: ask-before-deleting\n  Ask before running a command that deletes files. Why: the user rejected rm -rf and said so."],
    },
    "51aa7c66": {
        "Summary": ["Rebuilt the notes site. Claude asked before clearing the output folder and the user confirmed that asking first is the rule."],
        "Instructions the user repeated or emphasized": ['"Ask before running anything destructive ... Always." (x1)'],
        "Decisions and state changes": ["[2026-11-09] Site regenerated, 48 pages."],
        "Candidate memories": ["type: feedback; scope: global; slug: ask-before-deleting\n  Ask before destructive commands. Why: stated with \"Always\" in a second project."],
    },
}
SECTIONS = ["Summary", "Corrections from the user", "Instructions the user repeated or emphasized", "Tool and environment failures",
            "Decisions and state changes", "Contradictions with the current memory index", "Candidate memories"]


def memfile(name: str, description: str, kind: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: {kind}\n---\n\n{body}\n"


INDEX = ("# Memory Index\n\n"
         "- [Deploy steps](deploy-steps.md) — staging with make, then promote to production by hand\n"
         "- [Database migrations](db-migrations.md) — run migrations before every deploy; never edit an applied one\n"
         "- [Node 16 pin](node-16-pin.md) — keep Node pinned to 16 until the bundler bug is fixed\n")
MEMORY = {
    "deploy-steps.md": memfile("deploy-steps", "How acme-api is deployed: make deploy-staging, then promote to production by hand", "project",
        "Deploy to staging with `make deploy-staging`. When staging looks right, promote the build to\nproduction by hand from the dashboard. There is no production deploy script yet.\n\n"
        "**Why:** production deploys are rare and the team wants a person to press the button.\n"
        "**How to apply:** never run a production deploy yourself; stop after staging and say so."),
    "db-migrations.md": memfile("db-migrations", "Run database migrations before every deploy; never edit a migration that has been applied", "project",
        "Migrations live in `migrations/` and run with `make migrate`. Run them before every deploy.\n\n"
        "**Why:** an edited migration diverges from what production already ran.\n"
        "**How to apply:** to change a table, add a new migration; never edit an applied one."),
    "node-16-pin.md": memfile("node-16-pin", "Keep Node pinned to 16 in the build until the bundler bug is fixed", "project",
        "The asset build breaks on Node 18 and later because of a bundler bug. Keep `.nvmrc` at 16.\n\n"
        "**Why:** the build fails with a hashing error on newer Node versions.\n"
        "**How to apply:** do not raise the Node version when updating dependencies."),
}
DEPLOY_NEW = memfile("deploy-steps", "How acme-api is deployed: CI deploys to production when a version tag is pushed", "project",
    "CI deploys acme-api. Pushing a version tag (`v1.4.0`) runs the pipeline: it deploys to staging,\nruns the smoke tests, then promotes to production. Nobody deploys by hand any more.\n\n"
    "**Why:** hand promotion was slow and got skipped under pressure.\n"
    "**How to apply:** to release, tag and push. Do not run `make deploy-staging` or promote from the dashboard.")
TESTS_NEW = memfile("run-tests-first", "Run the test suite before every commit", "feedback",
    "Run the tests before committing, every time, and only commit when they pass.\n\n"
    "**Why:** the user corrected commit-before-test in three sessions (\"I keep having to say this\").\n"
    "**How to apply:** `pytest -q` first; if anything fails, fix it before `git commit`.")
ASK_NEW = ("---\nname: ask-before-deleting\ndescription: Ask before running any command that deletes files or data\nmetadata:\n  type: feedback\n---\n\n"
           "## Context / Trigger\n\nYou are about to run a command that deletes or overwrites files, folders or data.\n\n"
           "## Core Rule / Insight\n\nAsk first, say what will be removed, and wait for a yes.\n\n"
           "## Expected Outcome\n\nNothing is deleted that the user wanted to keep.\n")
GLOBAL_OLD = ("---\nname: plain-commit-messages\ndescription: Write commit messages as one plain sentence in the imperative\nmetadata:\n  type: feedback\n---\n\n"
              "## Context / Trigger\n\nYou are writing a commit message.\n\n## Core Rule / Insight\n\nOne plain sentence, imperative mood, no prefixes.\n\n"
              "## Expected Outcome\n\nThe log reads as a list of what changed.\n")
CLAUDE_MD = "# User-level instructions\n\n## Memories\n\n@~/.claude/memories/plain-commit-messages.md\n"


def proposals() -> tuple[list, list]:
    def ev(sid, day, quote):
        return {"session": next(s for _, s, _, _ in SESSIONS if s.startswith(sid)), "when": f"2026-11-{day:02d}", "quote": quote}
    project = [
        {"store": ACME, "action": "update", "verb": "verify", "file": "deploy-steps.md", "title": "Deploys go through CI now",
         "summary": "Replace the hand-promotion steps with the tag-push pipeline the user described.",
         "rationale": "The memory tells Claude to deploy to staging and promote by hand. On 4 November the user stopped exactly that and said CI deploys on a tag push.",
         "evidence": [ev("8b1d44e2", 4, "Stop. We don't deploy by hand any more. CI deploys when you push a tag.")],
         "prevalence": {"sessions": 1, "of": 4}, "confidence": "high", "proposed": DEPLOY_NEW,
         "index_line": "- [Deploy steps](deploy-steps.md) — CI deploys on a version tag; nobody deploys by hand"},
        {"store": ACME, "action": "add", "verb": "enrich", "file": "run-tests-first.md", "title": "Run the tests before committing",
         "summary": "A new feedback memory: run the suite before every commit.",
         "rationale": "The user made the same correction in three of four sessions, the last time adding \"I keep having to say this\". No memory holds it.",
         "evidence": [ev("3f2a91c0", 2, "Run the tests before you commit. Always."), ev("c07e5a13", 6, "Did you run the tests first? Run them before every commit."),
                      ev("e94b02fd", 8, "Tests first, then commit. I keep having to say this.")],
         "prevalence": {"sessions": 3, "of": 4}, "confidence": "high", "proposed": TESTS_NEW,
         "index_line": "- [Run tests first](run-tests-first.md) — run the suite before every commit; commit only when it passes", "promote": False},
        {"store": ACME, "action": "delete", "verb": "verify", "file": "node-16-pin.md", "title": "The Node 16 pin looks obsolete",
         "summary": "Archive the memory that says to keep Node pinned to 16.",
         "rationale": "On 6 November the user said the project moved to Node 20 and the pin was removed from the build script.",
         "evidence": [ev("c07e5a13", 6, "We moved to Node 20 last month. You can stop pinning Node 16 in the build.")],
         "prevalence": {"sessions": 1, "of": 4}, "confidence": "medium"},
    ]
    glob = [
        {"store": "global", "action": "add", "verb": "enrich", "file": "ask-before-deleting.md", "title": "Ask before deleting anything",
         "summary": "A rule for every project: ask before running a command that deletes files or data.",
         "rationale": "Stated in two unrelated projects, once after the user refused an rm -rf. It is about how the user works, not about one codebase.",
         "evidence": [ev("e94b02fd", 8, "Ask me before you delete anything."), ev("51aa7c66", 9, "Ask before running anything destructive, like you just did. Always.")],
         "prevalence": {"sessions": 2, "of": 5}, "confidence": "high", "proposed": ASK_NEW, "import_line": "@~/.claude/memories/ask-before-deleting.md"},
    ]
    return project, glob


# ----------------------------------------------------------------------------


def transcript(store: str, sid: str, day: int, mb: float) -> list[str]:
    short = sid[:8]
    cwd = "/home/sam/acme-api" if store == ACME else "/home/sam/notes-site"
    base = {"sessionId": sid, "cwd": cwd, "gitBranch": "main", "version": "2.1.320"}
    human = {"origin": {"kind": "human"}, "turnOrigin": "human"}
    out, n, minute = [], 0, 0

    def stamp():
        nonlocal minute
        minute += 2
        return f"2026-11-{day:02d}T{15 + minute // 60:02d}:{minute % 60:02d}:00.000Z"

    def emit(entry):
        out.append(json.dumps(entry))

    for turn in TURNS[short]:
        kind = turn[0]
        n += 1
        if kind == "user":
            emit({"type": "user", "uuid": f"u{n}", "timestamp": stamp(), **base, **human, "message": {"role": "user", "content": turn[1]}})
        elif kind == "queued":
            emit({"type": "attachment", "timestamp": stamp(), **base, "attachment": {
                "type": "queued_command", "prompt": turn[1], "source_uuid": f"q{n}", "commandMode": "prompt",
                "origin": {"kind": "human"}, "humanTurn": True}})
        elif kind == "claude":
            emit({"type": "assistant", "timestamp": stamp(), **base, "message": {"role": "assistant", "content": [{"type": "text", "text": turn[1]}]}})
        else:
            tid = f"t{n}"
            emit({"type": "assistant", "timestamp": stamp(), **base, "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": turn[1], "input": turn[2]}]}})
            if kind == "reject":
                body = ("The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, the "
                        "new_string was NOT written to the file). STOP what you are doing and wait for the user to tell you how to proceed. "
                        "To tell you how to proceed, the user said:\n" + turn[3])
                emit({"type": "user", "timestamp": stamp(), **base, "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "is_error": True, "content": body}]}})
            else:
                emit({"type": "user", "timestamp": stamp(), **base, "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})
    # Real transcripts are megabytes of tool output. Pad with successful tool results, which
    # the extractor drops, until the file is the size the story says.
    filler = "tests/test_orders.py::test_case PASSED\n" * 1600
    size, k = sum(len(x) + 1 for x in out), 0
    while size < mb * 1e6:
        k += 1
        a = json.dumps({"type": "assistant", "timestamp": stamp(), **base, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"p{k}", "name": "Bash", "input": {"command": "pytest -q"}}]}})
        b = json.dumps({"type": "user", "timestamp": stamp(), **base, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"p{k}", "content": filler}]}})
        out += [a, b]
        size += len(a) + len(b) + 2
    return out


def digest_body(short: str) -> str:
    d = DIGESTS[short]
    lines = []
    for s in SECTIONS:
        lines.append(f"## {s}")
        items = d.get(s)
        if s == "Summary":
            lines.append(items[0])
        elif items:
            lines += [f"- {x}" for x in items]
        else:
            lines.append("- none")
        lines.append("")
    return "\n".join(lines)


def put(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class World:
    """The throwaway folder, and a runner that points dreamctl at it."""

    def __init__(self, base: Path):
        self.base = base
        self.home = base / "home"
        self.root = base / "recensio"          # where digests/, runs/, reports/ ... go
        self.claude = self.home / ".claude"
        self.mem = self.claude / "projects" / ACME / "memory"
        self.story: dict = {}

    def build(self) -> None:
        old = time.time() - 7200               # not "still open": two hours since the last write
        for store, sid, day, mb in SESSIONS:
            f = self.claude / "projects" / store / f"{sid}.jsonl"
            put(f, "\n".join(transcript(store, sid, day, mb)) + "\n")
            os.utime(f, (old, old))
        put(self.mem / "MEMORY.md", INDEX)
        for name, text in MEMORY.items():
            put(self.mem / name, text)
        put(self.claude / "memories" / "plain-commit-messages.md", GLOBAL_OLD)
        put(self.claude / "CLAUDE.md", CLAUDE_MD)
        self.root.mkdir(parents=True, exist_ok=True)

    def ctl(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
        r = subprocess.run([sys.executable, str(DREAMCTL), "--root", str(self.root), *args],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        if r.returncode != 0:
            raise SystemExit(f"dreamctl {' '.join(args)} failed:\n{r.stderr}")
        return r

    def clean(self, text: str) -> str:
        """Shorten paths for display: the throwaway folder becomes relative, as in a real checkout."""
        for root, repl in ((self.root, ""), (self.home, "~"), (REPO, "")):
            for form in (str(root), root.as_posix()):
                text = text.replace(form + "\\", repl + ("/" if repl else "")).replace(form + "/", repl + ("/" if repl else "")).replace(form, repl or ".")
        return re.sub(r"(?<=[\w.~-])\\(?=[\w.-])", "/", text)

    # --- the real pipeline, with hand-written worker outputs --------------------------------

    def digest(self) -> None:
        st = self.story
        st["pending"] = self.clean(self.ctl("pending").stdout)
        prep = json.loads(self.ctl("digest-prepare").stdout)
        run = Path(prep["run"])
        man = json.loads((run / "digest-manifest.json").read_text(encoding="utf-8"))
        chunk = Path(next(s for s in man["sessions"] if s["session"].startswith("3f2a91c0"))["chunks"][0]).read_text(encoding="utf-8")
        for s in man["sessions"]:              # the workers' part
            put(Path(s["staged"]), digest_body(s["session"][:8]))
        fin = self.ctl("digest-finish", "--run", str(run))
        finj = json.loads(fin.stdout)
        sizes = {p.stem[:8]: p.stat().st_size for p in (self.root / "digests").rglob("*.md")}
        st.update({
            "sessions": [{"id": sid[:8], "mb": mb, "project": "acme-api" if store == ACME else "notes-site"} for store, sid, _, mb in SESSIONS],
            "pending_sessions": prep["pending_sessions"], "pending_mb": prep["pending_mb"], "batch_agents": prep["batch_agents"],
            "sealed": finj["sealed"], "totals": finj["totals"], "finish_line": fin.stderr.strip().splitlines()[-1],
            "digest_kb": {k: round(v / 1000, 1) for k, v in sizes.items()}, "digest_kb_total": round(sum(sizes.values()) / 1000, 1),
            "chunk_turns": chunk_turns(chunk), "digest_sample": {s: DIGESTS["3f2a91c0"].get(s, []) for s in SECTIONS},
        })

    def dream(self) -> Path:
        st = self.story
        prep = json.loads(self.ctl("dream-prepare", f"--scope={ACME},global").stdout)
        run = Path(prep["run"])
        project, glob = proposals()            # the dreamers' part
        put(run / f"proposals-{ACME}.json", json.dumps(project, indent=1))
        put(run / "proposals-global.json", json.dumps(glob, indent=1))
        asm = json.loads(self.ctl("assemble", "--run", str(run)).stdout)
        rep = Path(asm["report"])
        report = json.loads((rep / "proposals.json").read_text(encoding="utf-8"))
        st.update({
            "dream_stores": prep["stores"], "dream_agents": prep["agents"],
            "report": self.clean(asm["report"]), "problems": len(asm["problems"]), "n_proposals": asm["proposals"],
            "memory_before": sorted(p.name for p in self.mem.glob("*.md")),
            "proposals": [{"id": p["id"], "action": p["action"], "verb": p["verb"], "file": p["file"], "store": p["store"], "title": p["title"],
                           "quote": p["evidence"][0]["quote"], "sessions": p["prevalence"]["sessions"], "of": p["prevalence"]["of"],
                           "confidence": p["confidence"]} for p in report["proposals"]],
        })
        return rep

    def apply(self, rep: Path) -> None:
        st = self.story
        st["index_before"] = (self.mem / "MEMORY.md").read_text(encoding="utf-8").splitlines()
        st["dry_run"] = self.clean(self.ctl("apply", str(rep), "--dry-run").stdout).rstrip().splitlines()
        st["apply"] = self.clean(self.ctl("apply", str(rep)).stdout).rstrip().splitlines()
        applied = json.loads((rep / "applied.json").read_text(encoding="utf-8"))
        decisions = json.loads((rep / "decisions.json").read_text(encoding="utf-8"))["decisions"]
        for p in st["proposals"]:
            p["decision"] = decisions.get(p["id"], {}).get("decision", "skip")
            p["edited"] = "edited" in decisions.get(p["id"], {})
        st.update({
            "stamp": applied["snapshot"], "index_after": (self.mem / "MEMORY.md").read_text(encoding="utf-8").splitlines(),
            "memory_after": sorted(p.name for p in self.mem.glob("*.md")),
            "global_after": sorted(p.name for p in (self.claude / "memories").glob("*.md")),
            "claude_md_after": (self.claude / "CLAUDE.md").read_text(encoding="utf-8").splitlines(),
            "snapshot_files": sorted(p.name for p in (self.root / "snapshots" / applied["snapshot"] / ACME).glob("*.md")),
        })


def chunk_turns(chunk: str) -> list[dict]:
    """The first turns of a filtered transcript chunk, as (kind, text) for the digest clip."""
    body = chunk.split("failed results.\n\n", 1)[-1]
    turns = []
    for block in re.split(r"\n(?=\[\d{4}-\d\d-\d\d \d\d:\d\d\] )", body):
        m = re.match(r"\[[^\]]+\] (USER \([^)]*\)|USER REJECTED TOOL CALL \w+|USER|CLAUDE|TOOL ERROR \w+|TOOL)(?::|,)?\s*(.*)", block, re.S)
        if not m:
            continue
        kind, text = m.group(1), m.group(2).strip()
        if kind == "TOOL":
            name, _, raw = text.partition(" ")
            try:
                arg = next(iter(json.loads(raw).values()))
            except (ValueError, StopIteration):
                arg = raw
            text = f"{name}  {arg}"
        turns.append({"kind": kind, "text": text.replace("saying:\n", "").strip()})
    return turns
