#!/usr/bin/env python3
"""Self-test for dreamctl. Stdlib only, no model, no real memory store.

Builds a scratch world in a temp folder (fake transcripts, a fake project store, a fake
global store and user CLAUDE.md), points dreamctl at it with `--root`, and drives the whole
pipeline with hand-written worker outputs:

    extract -> digest-prepare -> digest-finish -> dream-prepare -> assemble -> decide -> apply

Run:  python tools/selftest.py        (exit code 0 = every check passed)
      python tools/selftest.py -k     (keep the scratch folder and print its path)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import dreamctl  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(cond, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print("  FAIL:", msg)


def ctl(root: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, str(TOOLS / "dreamctl.py"), "--root", str(root), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if ok and r.returncode != 0:
        FAILS.append(f"dreamctl {' '.join(args)} exited {r.returncode}: {r.stderr.strip()[-400:]}")
        print("  FAIL:", FAILS[-1])
    return r


def first_json(text: str) -> dict:
    return json.JSONDecoder().raw_decode(text[text.index("{"):])[0]


# ----------------------------------------------------------------------------
# the scratch world
# ----------------------------------------------------------------------------

SESSION = "aaaaaaaa-1111-2222-3333-444444444444"
EMPTY = "bbbbbbbb-1111-2222-3333-444444444444"
STORE = "P--Alpha"
REJECT = ("The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file "
          "edit, the new_string was NOT written to the file). STOP what you are doing and wait for the user to "
          "tell you how to proceed.")
DIGEST_BODY = ("## Summary\nA synthetic session.\n\n"
               "## Corrections from the user\n- [2026-11-01 10:00] \"stop doing that\" — Claude had X; the user wanted Y.\n\n"
               "## Instructions the user repeated or emphasized\n- none\n\n"
               "## Tool and environment failures\n- Bash: boom — unresolved (x1)\n\n"
               "## Decisions and state changes\n- [2026-11-01] chose tabs\n\n"
               "## Contradictions with the current memory index\n- none\n\n"
               "## Candidate memories\n- type: feedback; scope: global; slug: use-tabs\n  Use tabs. Why: said so.\n")
INDEX = "# Memory Index\n\n- [A](a.md) — old hook a\n- [B](b.md) — hook b\n- [Old](old.md) — hook old\n"
REWRITE = "# Memory Index\n\n- [A](a.md) — new hook a\n- [B](b.md) — hook b\n- [New](new.md) — hook new\n"


def claude_md(w: dict) -> str:
    """The fake user CLAUDE.md; import lines use the prefix dreamctl derives from config.global_store."""
    return (f"# User-level instructions\n\n## Memories\n\n{w['prefix']}g-keep.md\n"
            f"{w['prefix']}g-old.md\n\n### A note inside the section\n\nstays put\n\n## Other\n\nkeep me\n")


def mem(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: about {name}\nmetadata:\n  type: project\n---\n\n{body}\n"


def transcript() -> list[dict]:
    t = "2026-11-01T16:00:{:02d}.000Z"
    base = {"sessionId": SESSION, "cwd": "X:\\alpha", "gitBranch": "main", "version": "9.9.9"}
    human = {"origin": {"kind": "human"}, "turnOrigin": "human"}
    e: list[dict] = []

    def user(i, content, **kw):
        e.append({"type": "user", "timestamp": t.format(i), "uuid": f"u{i}", "message": {"role": "user", "content": content}, **base, **kw})

    user(1, "<command-message>skill</command-message>\n<command-name>/skill</command-name>\n<command-args>go now</command-args>", **human)
    user(2, [{"type": "text", "text": "Base directory for this skill: ... long skill body"}], isMeta=True)
    e.append({"type": "assistant", "timestamp": t.format(3), **base, "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Working on it."},
        {"type": "tool_use", "id": "t1", "name": "AskUserQuestion", "input": {"q": "Tabs or spaces?"}},
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "false"}},
        {"type": "tool_use", "id": "t3", "name": "Write", "input": {"file_path": "x"}}]}})
    user(4, [{"type": "tool_result", "tool_use_id": "t1", "content":
              'Your questions have been answered: "Tabs or spaces?"="Tabs". You can now continue with these answers in mind.'}])
    user(5, [{"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "boom"}])
    user(6, [{"type": "tool_result", "tool_use_id": "t3", "is_error": True,
              "content": REJECT + " To tell you how to proceed, the user said:\nno, use tabs"}])
    e.append({"type": "attachment", "timestamp": t.format(7), **base, "attachment": {
        "type": "queued_command", "prompt": "stop doing that", "source_uuid": "u15", "commandMode": "prompt",
        "origin": {"kind": "human"}, "timestamp": t.format(7), "humanTurn": True}})
    e.append({"type": "attachment", "timestamp": t.format(8), **base, "attachment": {
        "type": "queued_command", "prompt": "<task-notification>done</task-notification>", "source_uuid": "n1",
        "commandMode": "task-notification", "origin": {"kind": "task-notification"}}})
    user(9, [{"type": "text", "text": "[Request interrupted by user]"}])
    user(10, "<command-name>/effort</command-name>\n<command-message>effort</command-message>\n<command-args></command-args>")
    user(11, "Another Claude session sent a message: hello", origin={"kind": "peer"})
    for i in range(12, 15):
        user(i, f"plain human message {i} " + "x" * 900, **human)
    user(15, "stop doing that", **human)   # same message as the queued attachment above: rendered once
    user(16, "This session is being continued from a previous conversation. Earlier: [Request interrupted by user]", isCompactSummary=True)
    # request blobs replay earlier messages; they must not be counted again
    e.append({"type": "api-request-blob", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "boom"}]}})
    e.append({"type": "assistant", "timestamp": t.format(17), **base, "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Done. {CHUNK} {LINES} braces must survive."}]}})
    return e


def build_world(tmp: Path) -> dict:
    w = {"root": tmp / "root", "projects": tmp / "projects", "global": tmp / "global", "claude_md": tmp / "CLAUDE.md"}
    w["mem"] = w["projects"] / STORE / "memory"
    w["prefix"] = "@" + w["global"].as_posix() + "/"
    for k in ("root", "global", "mem"):
        w[k].mkdir(parents=True)
    cfg = json.loads((dreamctl.HOME / "config.json").read_text(encoding="utf-8"))
    cfg.update({"projects_root": str(w["projects"]), "global_store": str(w["global"]),
                "user_claude_md": str(w["claude_md"]), "exclude_name_patterns": [], "chunk_chars": 2000,
                "active_session_minutes": 0,   # the fake transcripts were written a moment ago

                "workers": {"digest": {"model": "sonnet"}, "dream": {"agentType": "x-agent", "bogus": 1}, "global": {}}})
    w["config"] = cfg
    (w["root"] / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    with (w["projects"] / STORE / f"{SESSION}.jsonl").open("w", encoding="utf-8") as fh:
        for entry in transcript():
            fh.write(json.dumps(entry) + "\n")
    (w["projects"] / "P--Empty").mkdir()
    (w["projects"] / "P--Empty" / f"{EMPTY}.jsonl").write_text(
        json.dumps({"type": "mode", "sessionId": EMPTY}) + "\n" + json.dumps({"type": "cost-state"}) + "\n", encoding="utf-8")
    reset_stores(w)
    return w


def reset_stores(w: dict) -> None:
    for d in (w["mem"], w["global"]):
        shutil.rmtree(d)
        d.mkdir(parents=True)
    put = dreamctl.write_text   # LF on every platform, so byte comparisons below hold
    put(w["mem"] / "MEMORY.md", INDEX)
    for n in ("a", "b", "old", "stray"):
        put(w["mem"] / f"{n}.md", mem(n, f"body of {n}"))
    for n in ("g-keep", "g-old"):
        put(w["global"] / f"{n}.md", f"## Context / Trigger\n\n{n}\n")
    put(w["claude_md"], claude_md(w))
    for d in ("reports", "snapshots", "archive"):
        shutil.rmtree(w["root"] / d, ignore_errors=True)


# ----------------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------------


def test_extract_and_digest(w: dict) -> None:
    print("extract / digest-prepare / digest-finish")
    root = w["root"]
    r = ctl(root, "digest-prepare")
    s = first_json(r.stdout)
    check(s.get("pending_sessions") == 2 and s.get("batch_stubs") == 1, f"prepare counts: {s}")
    run = Path(s["run"])
    man = json.loads((run / "digest-manifest.json").read_text(encoding="utf-8"))
    sess = next(x for x in man["sessions"] if x["session"] == SESSION)
    meta = json.loads(Path(sess["meta"]).read_text(encoding="utf-8"))
    # human turns: the slash command, the queued message, the interrupt and three plain messages
    want = {"human_turns": 6, "slash_commands": 1, "queued_turns": 1, "question_answers": 1,
            "tool_rejections": 1, "tool_errors": 1, "compactions": 1, "tool_calls": 3}
    for k, v in want.items():
        check(meta[k] == v, f"meta.{k} = {meta[k]}, want {v}")
    text = "".join(Path(c).read_text(encoding="utf-8") for c in sess["chunks"])
    for needle in ("USER (slash command):\n/skill go now", "USER (typed while Claude was working):\nstop doing that",
                   "USER (answered Claude's question):\n\"Tabs or spaces?\"=\"Tabs\"",
                   "USER REJECTED TOOL CALL Write, saying:\nno, use tabs", "TOOL ERROR Bash: boom",
                   "USER:\n[Request interrupted by user]", "COMPACTION SUMMARY:", "{CHUNK} {LINES} braces must survive"):
        check(needle in text, f"chunk text lacks {needle!r}")
    for bad in ("long skill body", "/effort", "Another Claude session", "task-notification"):
        check(bad not in text, f"chunk text should not contain {bad!r}")
    check(text.count("stop doing that") == 1, "queued message rendered twice")
    check(len(sess["chunks"]) > 1, "expected a multi-chunk session at chunk_chars=2000")
    n = len(sess["chunks"])
    for i, c in enumerate(sess["chunks"], 1):
        lines = Path(c).read_text(encoding="utf-8").split("\n")
        check(lines[-2] == f"END OF CHUNK {i} of {n}", f"chunk {i} end marker: {lines[-2]!r}")
        check(f"This file has {len(lines) - 1} lines" in lines[0], f"chunk {i} line count: {lines[0][:80]}")
    args = json.loads(Path(s["workflow_args_file"]).read_text(encoding="utf-8"))
    check(args == {"run": dreamctl.posix(run), "worker": {"model": "sonnet"}, "jobs": [[f"alpha/{SESSION[:8]}", n]]},
          f"workflow args: {args}")
    briefs = sorted(p.name for p in (run / "jobs").glob("*.md"))
    check(briefs == [f"001-c{k:02d}.md" for k in range(1, n + 1)] + ["001-merge.md"], f"briefs: {briefs}")
    check(dreamctl.posix(sess["staged"]) in (run / "jobs" / "001-merge.md").read_text(encoding="utf-8"), "merge brief names the staged path")

    # a worker that wraps its file in a fence and invents frontmatter is still sealed correctly
    Path(sess["staged"]).parent.mkdir(parents=True, exist_ok=True)
    Path(sess["staged"]).write_text("```markdown\n---\nsource_size: 1,234\n---\n" + DIGEST_BODY + "```\n", encoding="utf-8")
    f = first_json(ctl(root, "digest-finish", "--run", str(run)).stdout)
    check(f["sealed"] == 2 and not f["missing"] and not f["malformed"], f"finish: {f}")
    check(f["totals"] == {"corrections": 1, "failures": 1, "candidates": 1}, f"totals: {f['totals']}")
    check(f["still_pending_in_scope"] == 0, "nothing should be pending after finish")
    check(f["commit"] is None, "a scratch root is not a git checkout: no commit hint")
    d = (root / "digests" / STORE / f"{SESSION}.md").read_text(encoding="utf-8")
    fm = dreamctl.parse_frontmatter(d)
    src = w["projects"] / STORE / f"{SESSION}.jsonl"
    check(fm.get("source_size") == str(src.stat().st_size) and fm.get("digest_version") == "1", f"stamp: {fm}")
    check("1,234" not in d and "```" not in d and d.count("## Summary") == 1, "worker frontmatter/fence not cleaned")
    stub = (root / "digests" / "P--Empty" / f"{EMPTY}.md").read_text(encoding="utf-8")
    check("Empty session" in stub and "empty-session stub" in stub, "stub digest")

    # grown transcript -> pending again; a malformed worker file is not sealed and stays pending
    with src.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "timestamp": "2026-11-01T17:00:00.000Z", "origin": {"kind": "human"},
                             "message": {"content": "one more thing"}}) + "\n")
    s2 = first_json(ctl(root, "digest-prepare").stdout)
    check(s2["pending_sessions"] == 1, f"grown session should be pending: {s2}")
    run2 = Path(s2["run"])
    sess2 = json.loads((run2 / "digest-manifest.json").read_text(encoding="utf-8"))["sessions"][0]
    Path(sess2["staged"]).parent.mkdir(parents=True, exist_ok=True)
    Path(sess2["staged"]).write_text("Sorry, I could not read the chunk.\n", encoding="utf-8")
    f2 = first_json(ctl(root, "digest-finish", "--run", str(run2)).stdout)
    check(f2["sealed"] == 0 and len(f2["malformed"]) == 1 and f2["still_pending_in_scope"] == 1, f"malformed: {f2}")
    check(dreamctl.parse_frontmatter((root / "digests" / STORE / f"{SESSION}.md").read_text(encoding="utf-8")).get("source_size")
          == fm["source_size"], "a failed re-digest must leave the old digest in place")
    # a run that was killed before digest-finish is sealed by the next prepare
    s3 = first_json(ctl(root, "digest-prepare").stdout)
    sess3 = json.loads((Path(s3["run"]) / "digest-manifest.json").read_text(encoding="utf-8"))["sessions"][0]
    Path(sess3["staged"]).parent.mkdir(parents=True, exist_ok=True)
    Path(sess3["staged"]).write_text(DIGEST_BODY, encoding="utf-8")
    s4 = first_json(ctl(root, "digest-prepare").stdout)
    check(s4["sealed_from_earlier_runs"] == 1 and s4["pending_sessions"] == 0, f"leftover sealing: {s4}")


def test_active_sessions(w: dict) -> None:
    print("digest: a session still being written to waits for a later run")
    import os
    import time
    root = w["root"]
    src = w["projects"] / STORE / f"{SESSION}.jsonl"
    with src.open("a", encoding="utf-8") as fh:   # grows past its digest, and is "open" right now
        fh.write(json.dumps({"type": "user", "timestamp": "2026-11-01T18:00:00.000Z", "origin": {"kind": "human"},
                             "message": {"content": "still typing"}}) + "\n")
    cfg_path = root / "config.json"
    cfg_path.write_text(json.dumps({**w["config"], "active_session_minutes": 10}), encoding="utf-8")
    try:
        s = first_json(ctl(root, "digest-prepare").stdout)
        check(s["pending_sessions"] == 0 and len(s["skipped_still_active"]) == 1 and "run" not in s,
              f"an active session must be skipped, not digested: {s}")
        check("active: skipped for now" in ctl(root, "pending").stdout, "pending lists the active session as skipped")
        s = first_json(ctl(root, "digest-prepare", "--sessions", SESSION[:8]).stdout)
        check(s["pending_sessions"] == 1, "a session named with --sessions is digested even while active")
        sess = json.loads((Path(s["run"]) / "digest-manifest.json").read_text(encoding="utf-8"))["sessions"][0]
        Path(sess["staged"]).parent.mkdir(parents=True, exist_ok=True)
        Path(sess["staged"]).write_text(DIGEST_BODY, encoding="utf-8")
        f = first_json(ctl(root, "digest-finish", "--run", s["run"]).stdout)
        check(f["sealed"] == 1 and f["still_pending_in_scope"] == 0, f"no loop: nothing is left pending after sealing: {f}")
        with src.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "mode"}) + "\n")
        check(first_json(ctl(root, "digest-prepare").stdout)["pending_sessions"] == 0, "grown but active: still skipped")
        old = time.time() - 3600
        os.utime(src, (old, old))
        s = first_json(ctl(root, "digest-prepare").stdout)
        check(s["pending_sessions"] == 1 and not s["skipped_still_active"], f"quiet for an hour: pending again: {s}")
    finally:
        cfg_path.write_text(json.dumps(w["config"], indent=2), encoding="utf-8")


def test_commit_hint(tmp: Path) -> None:
    print("commit hint: only when the data folders are tracked")
    if not shutil.which("git"):
        print("  (git not found: skipped)")
        return
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True)
    saved = dreamctl.ROOT
    dreamctl.ROOT = repo
    try:
        hint = dreamctl.commit_hint(["digests", "state.json"], "digest: 3 sessions (all)")
        check(hint == ["git add digests state.json", 'git commit -q -m "digest: 3 sessions (all)"'], f"tracked checkout: {hint}")
        (repo / ".gitignore").write_text("digests/\nreports/\n", encoding="utf-8")
        check(dreamctl.commit_hint(["digests", "state.json"], "m") is None, "ignored folder that does not exist yet: no hint")
        (repo / "digests").mkdir()
        check(dreamctl.commit_hint(["digests", "state.json"], "m") is None, "ignored folder: no hint")
        dreamctl.ROOT = tmp / "not-a-repo"
        dreamctl.ROOT.mkdir()
        check(dreamctl.commit_hint(["digests"], "m") is None, "not a git checkout: no hint")
    finally:
        dreamctl.ROOT = saved


def test_store_without_memory(w: dict) -> None:
    print("apply: first memory for a project that has none")
    root = w["root"]
    reset_stores(w)
    bare = w["projects"] / "P--Empty" / "memory"   # this project has a transcript and a digest, no memory folder
    shutil.rmtree(bare, ignore_errors=True)
    s = first_json(ctl(root, "dream-prepare", "--scope", "P--Empty").stdout)
    check(s["project_stores"] == 1 and s["stores"][0]["memory_files"] == 0, f"dream-prepare for a store with no memory: {s}")
    run = Path(s["run"])
    (run / "proposals-P--Empty.json").write_text(json.dumps([{
        "store": "P--Empty", "action": "add", "verb": "enrich", "file": "first.md", "title": "First",
        "summary": "s", "rationale": "because", "confidence": "high", "proposed": mem("first", "body of first"),
        "index_line": "- [First](first.md) — hook first"}]), encoding="utf-8")
    rep = Path(first_json(ctl(root, "assemble", "--run", str(run)).stdout)["report"])
    ctl(root, "decide", str(rep), "--id", "p001", "--decision", "accept")
    out = ctl(root, "apply", str(rep)).stdout
    check((bare / "first.md").read_text(encoding="utf-8") == mem("first", "body of first"), "the memory file is created")
    check((bare / "MEMORY.md").read_text(encoding="utf-8") == "# Memory Index\n\n- [First](first.md) — hook first\n",
          f"a fresh index is created: {out}")
    ap = json.loads((rep / "applied.json").read_text(encoding="utf-8"))
    check((root / "snapshots" / ap["snapshot"] / "P--Empty").is_dir(), "an empty snapshot marks that the store did not exist")
    shutil.rmtree(bare, ignore_errors=True)


def proposals(w: dict, run: Path) -> None:
    def p(action, file, **kw):
        d = {"store": STORE, "action": action, "verb": "organize", "file": file, "title": f"{action} {file}",
             "summary": "s", "rationale": "because", "confidence": "high", "evidence": [], "prevalence": {}}
        d.update(kw)
        return d
    project = [
        p("update", "MEMORY.md", proposed=REWRITE),
        p("add", "new.md", proposed=mem("new", "body of new"), index_line="- [New](new.md) — hook new"),
        {**p("add", "x.md", proposed="x"), "store": "global"},                      # dropped: foreign store
        p("add", "sub/x.md", proposed="x"),                                         # dropped: not a bare name
        p("update", "a.md", proposed=mem("a", "new body of a"), index_line="- [A](a.md) — new hook a"),
        p("delete", "old.md"),
        p("update", "ghost.md", proposed="x"),                                      # dropped: no such file
        "not an object",                                                           # dropped
        p("update", "a.md", proposed=mem("a", "rival body of a")),                  # same file twice
    ]
    glob = [
        {**p("add", "g-new.md", proposed="## Context / Trigger\n\nnew\n", import_line="@~/.claude/memories/g-new.md"), "store": "global"},
        {**p("delete", "g-old.md"), "store": "global"},
    ]
    (run / f"proposals-{STORE}.json").write_text(json.dumps(project), encoding="utf-8")
    (run / "proposals-global.json").write_text(json.dumps(glob), encoding="utf-8")


def fresh_report(w: dict) -> Path:
    reset_stores(w)
    s = first_json(ctl(w["root"], "dream-prepare", "--scope", f"{STORE},global").stdout)
    run = Path(s["run"])
    proposals(w, run)
    a = first_json(ctl(w["root"], "assemble", "--run", str(run)).stdout)
    return Path(a["report"])


def decide(w: dict, rep: Path, accept: list[str]) -> None:
    ids = [p["id"] for p in json.loads((rep / "proposals.json").read_text(encoding="utf-8"))["proposals"]]
    for i in ids:
        ctl(w["root"], "decide", str(rep), "--id", i, "--decision", "accept" if i in accept else "reject")


def index_of(w: dict) -> list[str]:
    return [l for l in (w["mem"] / "MEMORY.md").read_text(encoding="utf-8").splitlines() if l.startswith("- ")]


def test_dream_and_apply(w: dict) -> None:
    print("dream-prepare / assemble")
    root = w["root"]
    reset_stores(w)
    s = first_json(ctl(root, "dream-prepare", "--scope", f"{STORE},global").stdout)
    run = Path(s["run"])
    check(s["agents"] == 2 and s["global_pass"] is True, f"dream-prepare: {s}")
    args = json.loads(Path(s["workflow_args_file"]).read_text(encoding="utf-8"))
    check(args == {"run": dreamctl.posix(run), "workers": {"dream": {"agentType": "x-agent"}, "global": {}},
                   "projects": ["alpha"], "global": True}, f"dream args: {args}")
    packet = (run / "packets" / STORE / "packet.md").read_text(encoding="utf-8")
    check(packet.rstrip().endswith(f"END OF PACKET {STORE}") and "### DIGEST " + SESSION in packet and "body of stray" in packet, "project packet")
    gp = (run / "packets" / "global" / "packet.md").read_text(encoding="utf-8")
    check("slug: use-tabs" in gp and gp.rstrip().endswith("END OF PACKET global"), "global packet carries candidates")
    brief = (run / "jobs" / "dream-001.md").read_text(encoding="utf-8")
    check(dreamctl.posix(run / f"proposals-{STORE}.json") in brief and "X:\\alpha" in brief, "project brief")
    check(dreamctl.posix(run / f"proposals-{STORE}.json") in (run / "jobs" / "dream-global.md").read_text(encoding="utf-8"), "global brief lists project proposals")
    # a dreamer that wrote nothing is a problem, not a silent gap
    a0 = first_json(ctl(root, "assemble", "--run", str(run), "--date", "missing").stdout)
    check(a0["proposals"] == 0 and len(a0["problems"]) == 2, f"missing proposals files: {a0['problems']}")

    rep = fresh_report(w)
    r = json.loads((rep / "proposals.json").read_text(encoding="utf-8"))
    got = [(p["id"], p["action"], p["file"]) for p in r["proposals"]]
    check(got == [("p001", "update", "MEMORY.md"), ("p002", "add", "new.md"), ("p003", "update", "a.md"),
                  ("p004", "delete", "old.md"), ("p005", "update", "a.md"), ("p006", "add", "g-new.md"),
                  ("p007", "delete", "g-old.md")], f"assembled: {got}")
    check(len(r["problems"]) == 4, f"problems: {r['problems']}")
    check(r["stores"][STORE]["label"] == "alpha" and r["stores"]["global"]["label"] == "global", "store labels")
    # the worker wrote "@~/.claude/memories/g-new.md"; the import line is derived from config instead
    check(r["proposals"][5]["import_line"] == w["prefix"] + "g-new.md", f"import line: {r['proposals'][5].get('import_line')}")
    check("p003 targets the same file" in (r["proposals"][4].get("note") or ""), "duplicate target note")
    check((rep / "review.html").exists() and "/*__PROPOSALS__*/null" not in (rep / "review.html").read_text(encoding="utf-8"), "review.html rendered")
    check(first_json(ctl(root, "report").stdout)["report"] == dreamctl.posix(rep), "report latest")

    print("apply: dry run writes nothing")
    decide(w, rep, ["p001", "p002", "p003", "p004"])
    before = {p.name: p.read_bytes() for p in w["mem"].iterdir()}
    out = ctl(root, "apply", str(rep), "--dry-run").stdout
    check("would add" in out and {p.name: p.read_bytes() for p in w["mem"].iterdir()} == before, "dry run changed the store")
    check((rep / "apply-dryrun.json").exists() and not (rep / "applied.json").exists() and not (root / "snapshots").exists(), "dry run artifacts")

    print("apply: lock refuses")
    (w["mem"] / ".consolidate-lock").write_text("", encoding="utf-8")
    r = ctl(root, "apply", str(rep), ok=False)
    check(r.returncode == 1 and "consolidate-lock" in r.stderr and {p.name: p.read_bytes() for p in w["mem"].iterdir() if p.name != ".consolidate-lock"} == before, "lock")
    (w["mem"] / ".consolidate-lock").unlink()

    print("apply: everything accepted")
    out = ctl(root, "apply", str(rep)).stdout
    check((w["mem"] / "MEMORY.md").read_text(encoding="utf-8") == REWRITE, f"index should equal the rewrite: {index_of(w)}")
    check("new body of a" in (w["mem"] / "a.md").read_text(encoding="utf-8") and (w["mem"] / "new.md").exists() and not (w["mem"] / "old.md").exists(), "files")
    ap = json.loads((rep / "applied.json").read_text(encoding="utf-8"))
    snap = root / "snapshots" / ap["snapshot"] / STORE
    check((snap / "old.md").exists() and "body of a" in (snap / "a.md").read_text(encoding="utf-8") and (snap / "MEMORY.md").read_text(encoding="utf-8") == INDEX, "snapshot holds the store as it was")
    arch = root / "archive" / ap["snapshot"] / STORE
    check((arch / "old.md").read_text(encoding="utf-8") == mem("old", "body of old") and "because" in (arch / "old.md.why.md").read_text(encoding="utf-8"), "archive")
    check(ap["stores"][STORE]["index_check"] == {"dangling": [], "unindexed": ["stray.md"]} and "files without an index line: stray.md" in out, f"index check: {ap['stores'][STORE]}")

    print("apply: rewrite accepted, the add and the delete it assumes rejected")
    rep = fresh_report(w)
    decide(w, rep, ["p001"])
    ctl(root, "apply", str(rep))
    check(index_of(w) == ["- [A](a.md) — new hook a", "- [B](b.md) — hook b", "- [Old](old.md) — hook old"], f"index: {index_of(w)}")
    check((w["mem"] / "old.md").exists() and not (w["mem"] / "new.md").exists(), "files untouched")

    print("apply: rewrite rejected, file changes accepted (one of two rivals wins)")
    rep = fresh_report(w)
    decide(w, rep, ["p002", "p003", "p004", "p005"])
    out = ctl(root, "apply", str(rep)).stdout
    check(index_of(w) == ["- [A](a.md) — new hook a", "- [B](b.md) — hook b", "- [New](new.md) — hook new"], f"index: {index_of(w)}")
    check("p005" in out and "another accepted proposal already targets this file" in out and "new body of a" in (w["mem"] / "a.md").read_text(encoding="utf-8"), "rival update skipped")

    print("apply: add accepted without an index line gets a default one")
    rep = fresh_report(w)
    r = json.loads((rep / "proposals.json").read_text(encoding="utf-8"))
    del r["proposals"][1]["index_line"]
    (rep / "proposals.json").write_text(json.dumps(r), encoding="utf-8")
    decide(w, rep, ["p002"])
    ctl(root, "apply", str(rep))
    check(index_of(w)[-1] == "- [add new.md](new.md) — about new", f"default index line: {index_of(w)[-1]}")

    print("apply: hash guard")
    rep = fresh_report(w)
    (w["mem"] / "a.md").write_text("edited by a live session\n", encoding="utf-8")
    dreamctl.write_text(w["mem"] / "MEMORY.md", INDEX + "- [Live](live.md) — added by a live session\n")
    decide(w, rep, ["p001", "p002", "p003"])
    out = ctl(root, "apply", str(rep)).stdout
    check(out.count("file changed since the dream") == 2 and (w["mem"] / "a.md").read_text(encoding="utf-8") == "edited by a live session\n", "guarded files must be left alone")
    check(index_of(w) == ["- [A](a.md) — old hook a", "- [B](b.md) — hook b", "- [Old](old.md) — hook old",
                          "- [Live](live.md) — added by a live session", "- [New](new.md) — hook new"], f"index: {index_of(w)}")

    print("apply: global store")
    rep = fresh_report(w)
    decide(w, rep, ["p006", "p007"])
    ctl(root, "apply", str(rep))
    md = w["claude_md"].read_text(encoding="utf-8")
    # the new import joins the section's other imports (above the nested heading); the old one is gone
    want = claude_md(w).replace(w["prefix"] + "g-old.md", w["prefix"] + "g-new.md")
    check(md == want, f"user CLAUDE.md:\n{md}")
    ap = json.loads((rep / "applied.json").read_text(encoding="utf-8"))
    check((w["global"] / "g-new.md").exists() and not (w["global"] / "g-old.md").exists()
          and (root / "archive" / ap["snapshot"] / "global" / "g-old.md").exists()
          and (root / "snapshots" / ap["snapshot"] / "global" / "CLAUDE.md.user").read_text(encoding="utf-8") == claude_md(w), "global files, archive and snapshot")
    check(INDEX == (w["mem"] / "MEMORY.md").read_text(encoding="utf-8"), "project store untouched by a global-only apply")
    r = ctl(root, "report", ok=False)
    check(r.returncode != 0 or first_json(r.stdout)["report"] != dreamctl.posix(rep), "an applied report is no longer 'latest'")


def test_global_store_options(w: dict) -> None:
    print("global store: import lines, missing CLAUDE.md, disabled")
    add = dreamctl._claude_md_add_import
    check(add("", "@x/y.md") == "## Memories\n\n@x/y.md\n", "import into an empty CLAUDE.md")
    check(add("# Mine\n\nhello\n", "@x/y.md", "## Notes") == "# Mine\n\nhello\n\n## Notes\n\n@x/y.md\n", "section is created")
    check(add("## Memories\n\n@x/y.md\n", "@x/y.md") == "## Memories\n\n@x/y.md\n", "import is idempotent")
    cfg_path = w["root"] / "config.json"
    cfg_path.write_text(json.dumps({**w["config"], "global_store": None}), encoding="utf-8")
    try:
        s = first_json(ctl(w["root"], "dream-prepare", "--scope", "all").stdout)
        check(s["global_pass"] is False and s["agents"] == 2, f"scope all without a global store: {s}")
        r = ctl(w["root"], "dream-prepare", "--scope", "global", ok=False)
        check(r.returncode != 0 and "disabled" in r.stderr, "scope global must fail when the global store is disabled")
        check("disabled" in ctl(w["root"], "projects").stdout, "projects says the global store is disabled")
    finally:
        cfg_path.write_text(json.dumps(w["config"], indent=2), encoding="utf-8")


def test_hyphen_store_names(w: dict) -> None:
    print("cli: store names that start with a hyphen (every store on macOS and Linux)")
    root = w["root"]
    unix = w["projects"] / "-home-sam-app"
    (unix / "memory").mkdir(parents=True)
    dreamctl.write_text(unix / "memory" / "MEMORY.md", "# Memory Index\n\n- [N](n.md) — hook\n")
    dreamctl.write_text(unix / "memory" / "n.md", mem("n", "body of n"))
    try:
        r = ctl(root, "pending", "--project", "-home-sam-app")
        check(r.returncode == 0 and "nothing pending" in r.stdout, f"pending --project -name: {r.stderr[-200:]}")
        s = first_json(ctl(root, "dream-prepare", "--scope", "-home-sam-app").stdout)
        check(s.get("project_stores") == 1 and s["stores"][0]["store"] == "-home-sam-app", f"dream-prepare --scope -name: {s}")
        run = Path(s["run"])
        check((run / "proposals--home-sam-app.json").name in (run / "jobs" / "dream-001.md").read_text(encoding="utf-8"), "brief names the proposals file")
        r = ctl(root, "dream-prepare", "--scope", "-no-such-store", ok=False)
        check(r.returncode != 0 and "no such project store" in r.stderr, f"unknown -name is reported, not an argparse error: {r.stderr[-200:]}")
        check(dreamctl._join_dash_values(["apply", "x", "--remap", "-a-b=/tmp/c", "--dry-run"]) == ["apply", "x", "--remap=-a-b=/tmp/c", "--dry-run"], "remap with a hyphen name")
        check(dreamctl._join_dash_values(["pending", "--project", "--json"]) == ["pending", "--project", "--json"], "a real flag is not swallowed")
    finally:
        shutil.rmtree(unix, ignore_errors=True)


def test_review_server(w: dict) -> None:
    print("serve: decisions over HTTP, Finish exits 0")
    import re
    import time
    import urllib.request
    rep = fresh_report(w)
    proc = subprocess.Popen([sys.executable, str(TOOLS / "dreamctl.py"), "--root", str(w["root"]), "serve", str(rep), "--no-browser"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        url = re.search(r"http://127\.0\.0\.1:\d+/", proc.stdout.readline()).group(0)

        def post(path, body):
            req = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as ex:
                return ex.code, {}

        page = urllib.request.urlopen(url, timeout=10).read().decode("utf-8")
        check("Dream review" in page and "p007" in page, "page serves the embedded proposals")
        check(post("decision", {"id": "p002", "decision": "maybe"})[0] == 400, "bad decision is rejected")
        check(post("decision", {"id": "p002", "decision": "accept", "edited": mem("new", "edited in the page")})[0] == 200, "accept with edit")
        check(post("decision", {"id": "p004", "decision": "reject"})[0] == 200, "reject")
        d = json.loads((rep / "decisions.json").read_text(encoding="utf-8"))
        check(d["decisions"]["p002"]["decision"] == "accept" and not d["finished"], "decisions are saved on every click")
        check(post("finish", {})[0] == 200, "finish")
        for _ in range(100):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        check(proc.poll() == 0, f"server exit code {proc.poll()}")
    finally:
        if proc.poll() is None:
            proc.kill()
    ctl(w["root"], "apply", str(rep))
    check("edited in the page" in (w["mem"] / "new.md").read_text(encoding="utf-8") and (w["mem"] / "old.md").exists(), "the edited text is what gets written")


def main() -> int:
    keep = "-k" in sys.argv[1:]
    tmp = Path(tempfile.mkdtemp(prefix="dreaming-selftest-"))
    try:
        w = build_world(tmp)
        test_extract_and_digest(w)
        test_active_sessions(w)
        test_commit_hint(tmp)
        test_dream_and_apply(w)
        test_store_without_memory(w)
        test_hyphen_store_names(w)
        test_global_store_options(w)
        test_review_server(w)
    finally:
        if keep:
            print("scratch world kept at", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
