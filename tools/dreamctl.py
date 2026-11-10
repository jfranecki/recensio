#!/usr/bin/env python3
"""dreamctl - the mechanical half of /digest, /dream and /review. Stdlib only.

Subcommands. The script finds its home from its own location, so it runs from any
directory. `--root DIR` (before the subcommand) points digests/, runs/, reports/,
snapshots/ and archive/ at another folder, for tests against a scratch copy.

  projects  [--json]                      list memory stores (projects + global)
  pending   [--project NAME|all] [--json] sessions that still need a digest

  digest-prepare [--project NAME|all] [--max-agents N] [--sessions ID,ID]
                                          one batch: extract chunks, write worker briefs,
                                          print the run dir and the workflow args file
  digest-finish --run DIR                 validate staged digests, stamp the watermark
                                          frontmatter, move them into digests/
  dream-prepare --scope SCOPE             write dream packets and worker briefs; SCOPE is
                                          all | global | STORE[,STORE...][,global]
  assemble  --run DIR [--date YYYY-MM-DD] merge worker proposals into reports/<date>/

  report    [REPORT_DIR|latest]           counts for a report and its decisions so far
  render    REPORT_DIR                    build review.html from proposals.json
  serve     [REPORT_DIR|latest] [--idle-minutes N]   local review page; exits on Finish
  decide    REPORT_DIR --id P --decision accept|reject|skip [--edited-file F] [--finish]
  apply     [REPORT_DIR|latest] [--dry-run] [--remap STORE=PATH]... [--user-claude-md F]

  extract   SESSION_JSONL --out DIR       (low level) chunks for one session
  extract-all --pending FILE --run DIR    (low level) chunks for a pending.json
  bundle    --scope SCOPE --run DIR       (low level) dream packets only

Nothing here calls a model. Agents do the reading and writing of digests and
proposals; this script finds, filters, packages, stamps, serves and applies.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import sys
import threading
import webbrowser
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent   # shipped tools/, prompts/, workflows/, config.json
ROOT = HOME                                     # data root; `--root` repoints it for tests

# ----------------------------------------------------------------------------
# config / paths
# ----------------------------------------------------------------------------


def load_config() -> dict:
    p = ROOT / "config.json"
    if not p.exists():
        p = HOME / "config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    # the import line a global memory gets in the user CLAUDE.md, e.g. "@~/.claude/memories/"
    raw = cfg.get("global_store")
    cfg["global_import_prefix"] = ("@" + raw.replace("\\", "/").rstrip("/") + "/") if raw else None
    cfg.setdefault("global_index_heading", "## Memories")
    for k in ("projects_root", "global_store", "user_claude_md"):
        cfg[k] = Path(os.path.expanduser(cfg[k])) if cfg.get(k) else None
    if cfg["global_store"] is None or cfg["user_claude_md"] is None:
        cfg["global_store"] = cfg["user_claude_md"] = None   # no global store: project stores only
    return cfg


def munged(path: Path | str) -> str:
    """The folder name Claude Code gives a project under ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def posix(p: Path | str) -> str:
    """Forward-slash form of a path, safe to paste into JSON and prompts on Windows."""
    return Path(p).as_posix()


def new_run_dir(kind: str) -> Path:
    base = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S") + "-" + kind
    run = ROOT / "runs" / base
    k = 2
    while run.exists():
        run = ROOT / "runs" / f"{base}-{k}"
        k += 1
    run.mkdir(parents=True)
    return run


def store_labels(rows: list[dict]) -> dict[str, str]:
    """A short display name per store: the project folder's name, else the store name with
    the longest other store name it starts with removed (a parent project). Cosmetic only."""
    names = [r["store"] for r in rows]
    out = {}
    for r in rows:
        store, path = r["store"], r.get("project_path")
        if path:
            label = re.split(r"[\\/]", path.rstrip("\\/"))[-1]
        else:
            parents = [n for n in names if n != store and store.startswith(n + "-")]
            label = store[len(max(parents, key=len)) + 1:] if parents else store
        out[store] = label or store
    return out


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> str:
    return dt.date.today().isoformat()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def write_text(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as fh:   # Path.write_text has no newline= before 3.10
        fh.write(s)


def iso_of_mtime(p: Path) -> str:
    return dt.datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML-ish frontmatter reader: top-level `key: value` lines only."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


# ----------------------------------------------------------------------------
# project discovery
# ----------------------------------------------------------------------------


def excluded(name: str, cfg: dict) -> bool:
    """Skipped unless named explicitly: this checkout's own sessions (they are the dreaming
    runs themselves) and anything matching `exclude_name_patterns`."""
    if name == munged(HOME):
        return True
    return any(re.search(p, name) for p in cfg.get("exclude_name_patterns", []))


def session_files(project_dir: Path) -> list[Path]:
    """Main-session transcripts only (subagent transcripts live in <session>/subagents/)."""
    return sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def real_path_of(project_dir: Path) -> str | None:
    """Recover the real project path from the newest transcript's first `cwd`."""
    for f in session_files(project_dir)[:3]:
        try:
            with f.open(encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh):
                    if i > 400:
                        break
                    if '"cwd"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = e.get("cwd")
                    if cwd:
                        return cwd
        except OSError:
            continue
    return None


def memory_dir_of(project_dir: Path) -> Path:
    return project_dir / "memory"


def list_projects(cfg: dict) -> list[dict]:
    root: Path = cfg["projects_root"]
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        sessions = session_files(d)
        mem = memory_dir_of(d)
        mem_files = sorted(p.name for p in mem.glob("*.md")) if mem.is_dir() else []
        has_store = bool(mem_files)
        if not sessions and not has_store:
            continue
        digest_dir = ROOT / "digests" / d.name
        digests = sorted(digest_dir.glob("*.md")) if digest_dir.is_dir() else []
        out.append({
            "store": d.name,
            "kind": "project",
            "project_path": real_path_of(d) if sessions else None,
            "memory_dir": str(mem),
            "has_store": has_store,
            "memory_files": len([f for f in mem_files if f != "MEMORY.md"]),
            "has_index": "MEMORY.md" in mem_files,
            "transcripts": len(sessions),
            "transcript_bytes": sum(p.stat().st_size for p in sessions),
            "digests": len(digests),
            "excluded": excluded(d.name, cfg),
        })
    return out


def global_store_info(cfg: dict) -> dict | None:
    g: Path | None = cfg["global_store"]
    if g is None:
        return None
    files = sorted(p.name for p in g.glob("*.md")) if g.is_dir() else []
    return {
        "store": "global",
        "kind": "global",
        "memory_dir": str(g),
        "index_file": str(cfg["user_claude_md"]),
        "memory_files": len(files),
        "excluded": False,
    }


def cmd_projects(args, cfg):
    rows = list_projects(cfg)
    if args.json:
        print(json.dumps({"global": global_store_info(cfg), "projects": rows}, indent=2))
        return
    g = global_store_info(cfg)
    print(f"global  {g['memory_dir']}  files={g['memory_files']}" if g else "global  (disabled in config.json)")
    for r in rows:
        flag = "EXCLUDED" if r["excluded"] else ""
        print(f"{r['store']:<60} transcripts={r['transcripts']:>3} ({r['transcript_bytes']/1e6:6.1f} MB)"
              f"  memory={r['memory_files']:>3}{'' if r['has_index'] else ' (no index)'}"
              f"  digests={r['digests']:>3}  {flag}")


# ----------------------------------------------------------------------------
# pending digests
# ----------------------------------------------------------------------------


def digest_path(store: str, session: str) -> Path:
    return ROOT / "digests" / store / f"{session}.md"


def pending_sessions(cfg: dict, project: str | None, only: list[str] | None = None,
                     include_active: bool = False) -> list[dict]:
    """Sessions with no current digest. `only` keeps sessions whose id starts with a given prefix.

    A transcript written to in the last `active_session_minutes` belongs to a session that is
    probably still open. Digesting it now would stamp a size it has already outgrown, and the
    next pass would digest it again, so such rows are left out unless `include_active` is set
    (each row carries `active` either way).
    """
    import time
    quiet = float(cfg.get("active_session_minutes", 10)) * 60
    now = time.time()
    rows = []
    for pr in list_projects(cfg):
        if pr["excluded"] and project != pr["store"]:
            continue
        if project and project != "all" and project != pr["store"]:
            continue
        d = cfg["projects_root"] / pr["store"]
        for f in session_files(d):
            sid = f.stem
            if only and not any(sid.startswith(x) for x in only):
                continue
            size = f.stat().st_size
            dp = digest_path(pr["store"], sid)
            reason = None
            if not dp.exists():
                reason = "new"
            else:
                fm = parse_frontmatter(read_text(dp))
                try:
                    if int(fm.get("digest_version", "0")) < int(cfg["digest_version"]):
                        reason = "version"
                    elif int(fm.get("source_size", "0")) < size:
                        reason = "grown"
                except ValueError:
                    reason = "unreadable-frontmatter"
            if reason:
                active = (now - f.stat().st_mtime) < quiet
                if active and not include_active:
                    continue
                rows.append({
                    "store": pr["store"],
                    "project_path": pr["project_path"],
                    "session": sid,
                    "path": str(f),
                    "size": size,
                    "mtime": iso_of_mtime(f),
                    "reason": reason,
                    "active": active,
                })
    return rows


def cmd_pending(args, cfg):
    rows = pending_sessions(cfg, args.project, include_active=True)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("nothing pending: every session has a current digest")
        return
    ready = [r for r in rows if not r["active"]]
    total = sum(r["size"] for r in ready)
    print(f"{len(ready)} sessions pending ({total/1e6:.1f} MB raw)"
          + (f"; {len(rows) - len(ready)} more still being written to, left for a later run" if len(ready) < len(rows) else ""))
    for r in rows:
        print(f"  {r['store']:<50} {r['session'][:8]}  {r['size']/1e6:6.1f} MB  {r['reason']}"
              + ("  (active: skipped for now)" if r["active"] else ""))


# ----------------------------------------------------------------------------
# transcript extraction
# ----------------------------------------------------------------------------

_NOISE_TAG = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|command-name|command-message|"
    r"command-args|user-prompt-submit-hook|task-notification|skill-format)\b.*?</\1>",
    re.S | re.I,
)
_NOISE_OPEN = re.compile(
    r"^\s*<(?:local-command-caveat|local-command-stdout|command-name|command-message|command-args|"
    r"system-reminder|user-prompt-submit-hook|task-notification)",
    re.I,
)


_CMD_NAME = re.compile(r"<command-name>\s*(/?[^<\s]+)\s*</command-name>", re.I)
_CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S | re.I)
_REJECT_HEAD = "The user doesn't want to proceed with this tool use"


def clean_human_text(s: str) -> str:
    s = _NOISE_TAG.sub("", s)
    return s.strip()


def slash_command_of(s: str) -> str | None:
    """`/name args` when the text is a slash-command invocation, else None."""
    m = _CMD_NAME.search(s)
    if not m:
        return None
    name = m.group(1) if m.group(1).startswith("/") else "/" + m.group(1)
    a = _CMD_ARGS.search(s)
    return (name + " " + (a.group(1).strip() if a else "")).strip()


def _origin_kind(obj: dict) -> str | None:
    o = obj.get("origin")
    if isinstance(o, dict) and o.get("kind"):
        return o["kind"]
    return obj.get("turnOrigin") or None


def _result_text(cc) -> str:
    if isinstance(cc, str):
        return cc
    if isinstance(cc, list):
        parts = [b.get("text", "") for b in cc if isinstance(b, dict) and b.get("type") == "text"]
        if parts:
            return "\n".join(parts)
    return json.dumps(cc, ensure_ascii=False)


def rejection_reason(body: str) -> str:
    """What the user typed when declining a tool call; empty when they only clicked no."""
    m = re.search(r"the user said:\s*(.*)", body, re.S | re.I)
    if m:
        return m.group(1).strip()
    rest = re.sub(r"^The user doesn't want to proceed with this tool use\..*?(?:how to proceed\.|\Z)", "",
                  body.strip(), flags=re.S)
    rest = re.sub(r"Note: The user's next message may contain a correction or preference\..*", "", rest, flags=re.S)
    return rest.strip()


def answer_text(body: str) -> str:
    s = re.sub(r"^(?:Your questions have been answered|User has answered your questions?):\s*", "", body.strip())
    return re.sub(r"\s*You can now continue with (?:these|the user's) answers in mind\.?\s*$", "", s).strip()


def _blocks(entry: dict) -> list:
    msg = entry.get("message") or {}
    c = msg.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    if isinstance(c, list):
        return [b for b in c if isinstance(b, dict)]
    return []


def _is_human_entry(entry: dict) -> bool:
    if entry.get("type") != "user":
        return False
    if entry.get("isMeta") or entry.get("isSidechain") or entry.get("isCompactSummary"):
        return False
    kind = _origin_kind(entry)
    if kind:
        return kind == "human"
    # no origin marker: local commands, interrupts ("[Request interrupted by user]") and
    # older transcripts; the noise filters downstream decide what survives
    return True


def _queued_is_human(a: dict) -> bool:
    """A `queued_command` attachment is a message the user typed while Claude was working."""
    kind = _origin_kind(a)
    if kind:
        return kind == "human"
    return a.get("commandMode") == "prompt" and not a.get("isMeta") and not a.get("reminderId")


def _stamp(entry: dict) -> str:
    return _stamp_ts(entry.get("timestamp") or "")


def _stamp_ts(ts: str) -> str:
    try:
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return d.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts[:16]


def _cap(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    return s[:n] + f" …[+{len(s)-n} chars]"


def extract_session(jsonl: Path, cfg: dict, store: str | None = None) -> tuple[list[str], dict]:
    """Return (turn_lines, meta). Each element of turn_lines is one rendered turn."""
    caps = cfg["caps"]
    turns: list[str] = []
    tool_names: dict[str, str] = {}
    seen_human: set[str] = set()   # uuids of human messages already rendered
    meta = {"session": jsonl.stem, "store": store or jsonl.parent.name, "cwd": None,
            "git_branch": None, "version": None, "first": None, "last": None,
            "human_turns": 0, "slash_commands": 0, "queued_turns": 0, "question_answers": 0,
            "tool_rejections": 0, "assistant_turns": 0, "tool_calls": 0, "tool_errors": 0,
            "compactions": 0, "source_size": jsonl.stat().st_size, "source_mtime": iso_of_mtime(jsonl)}
    with jsonl.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = e.get("type")
            if t == "attachment":
                # a message typed mid-turn is logged as a queued_command attachment, not a user entry
                a = e.get("attachment") or {}
                if a.get("type") != "queued_command" or not _queued_is_human(a):
                    continue
                sid = a.get("source_uuid")
                if sid and sid in seen_human:
                    continue
                p = a.get("prompt")
                raw = p if isinstance(p, str) else _result_text(p) if isinstance(p, list) else ""
                txt = slash_command_of(raw) or clean_human_text(raw)
                if not txt or _NOISE_OPEN.match(txt):
                    continue
                if sid:
                    seen_human.add(sid)
                meta["human_turns"] += 1
                meta["queued_turns"] += 1
                turns.append(f"[{_stamp_ts(a.get('timestamp') or e.get('timestamp') or '')}] "
                             f"USER (typed while Claude was working):\n{_cap(txt, caps['user_turn_chars'])}\n")
                continue
            if t not in ("user", "assistant"):
                continue
            if meta["cwd"] is None and e.get("cwd"):
                meta["cwd"] = e.get("cwd")
                meta["git_branch"] = e.get("gitBranch")
                meta["version"] = e.get("version")
            ts = e.get("timestamp")
            if ts:
                meta["first"] = meta["first"] or ts
                meta["last"] = ts
            stamp = _stamp(e)
            if e.get("isCompactSummary"):
                txt = " ".join(b.get("text", "") for b in _blocks(e) if b.get("type") == "text")
                meta["compactions"] += 1
                turns.append(f"[{stamp}] COMPACTION SUMMARY:\n{_cap(txt, caps['compact_summary_chars'])}\n")
                continue
            if t == "user":
                human = _is_human_entry(e)
                explicit_human = _origin_kind(e) == "human"
                uid = e.get("uuid")
                if human and uid and uid in seen_human:
                    human = False   # already rendered from its queued_command attachment
                for b in _blocks(e):
                    bt = b.get("type")
                    if bt == "tool_result":
                        name = tool_names.get(b.get("tool_use_id", ""), "?")
                        body = _result_text(b.get("content"))
                        if b.get("is_error") and body.lstrip().startswith(_REJECT_HEAD):
                            # the user declined the call: a correction signal, not a tool failure
                            meta["tool_rejections"] += 1
                            why = rejection_reason(body)
                            turns.append(f"[{stamp}] USER REJECTED TOOL CALL {name}"
                                         + (f", saying:\n{_cap(why, caps['user_turn_chars'])}" if why else "") + "\n")
                        elif b.get("is_error"):
                            meta["tool_errors"] += 1
                            turns.append(f"[{stamp}] TOOL ERROR {name}: {_cap(body, caps['tool_error_chars'])}\n")
                        elif name == "AskUserQuestion":
                            # the user's answers to Claude's questions are their own words
                            ans = answer_text(body)
                            if ans:
                                meta["question_answers"] += 1
                                turns.append(f"[{stamp}] USER (answered Claude's question):\n"
                                             f"{_cap(ans, caps['user_turn_chars'])}\n")
                    elif bt == "text" and human:
                        raw = b.get("text", "")
                        cmd = slash_command_of(raw) if explicit_human else None
                        if cmd:
                            # a skill or prompt command the user ran; local commands (/model, /effort)
                            # carry no origin marker and stay dropped
                            meta["human_turns"] += 1
                            meta["slash_commands"] += 1
                            turns.append(f"[{stamp}] USER (slash command):\n{_cap(cmd, caps['user_turn_chars'])}\n")
                            if uid:
                                seen_human.add(uid)
                            continue
                        txt = clean_human_text(raw)
                        if not txt or _NOISE_OPEN.match(txt):
                            continue
                        meta["human_turns"] += 1
                        if uid:
                            seen_human.add(uid)
                        turns.append(f"[{stamp}] USER:\n{_cap(txt, caps['user_turn_chars'])}\n")
            else:  # assistant
                if e.get("isSidechain"):
                    continue
                said = False
                for b in _blocks(e):
                    bt = b.get("type")
                    if bt == "text":
                        txt = b.get("text", "").strip()
                        if txt:
                            said = True
                            turns.append(f"[{stamp}] CLAUDE:\n{_cap(txt, caps['assistant_block_chars'])}\n")
                    elif bt == "tool_use":
                        name = b.get("name", "?")
                        tool_names[b.get("id", "")] = name
                        meta["tool_calls"] += 1
                        inp = json.dumps(b.get("input", {}), ensure_ascii=False)
                        turns.append(f"[{stamp}] TOOL {name} {_cap(inp, caps['tool_input_chars'])}\n")
                if said:
                    meta["assistant_turns"] += 1
    return turns, meta


def subagent_errors(jsonl: Path, cfg: dict) -> tuple[list[str], int]:
    """Grep subagent transcripts for failed tool results only."""
    sub = jsonl.parent / jsonl.stem / "subagents"
    if not sub.is_dir():
        return [], 0
    cap = cfg["caps"]["subagent_error_entries"]
    found: list[str] = []
    agents = 0
    for f in sorted(sub.glob("agent-*.jsonl")):
        agents += 1
        names: dict[str, str] = {}
        seen: set[str] = set()
        try:
            with f.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"tool_use"' not in line and '"is_error"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("type") not in ("user", "assistant"):
                        continue   # request blobs replay earlier messages; count each result once
                    for b in _blocks(e):
                        if b.get("type") == "tool_use":
                            names[b.get("id", "")] = b.get("name", "?")
                        elif b.get("type") == "tool_result" and b.get("is_error"):
                            rid = b.get("tool_use_id", "")
                            if rid in seen:
                                continue
                            seen.add(rid)
                            found.append(f"{f.stem}: {names.get(rid, '?')}: "
                                         f"{_cap(_result_text(b.get('content')), 200)}")
        except OSError:
            continue
    extra = max(0, len(found) - cap)
    found = found[:cap]
    if extra:
        found.append(f"…and {extra} more subagent tool errors not listed")
    return found, agents


def end_marker(i: int, n: int) -> str:
    return f"END OF CHUNK {i} of {n}"


def write_chunks(turns: list[str], top: str, rest: str, out: Path, chunk_chars: int) -> tuple[list[Path], list[int]]:
    """Write chunk files; return (paths, line counts).

    `top` is the first header line and carries {CHUNK} and {LINES}; `rest` is the remainder
    of the header and is written verbatim (it holds project text, which may contain braces).
    Every chunk ends with an END OF CHUNK line and states its own line count up top, so a
    worker whose Read call returned only part of the file can tell and keep reading.
    """
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("chunk-*.txt"):
        old.unlink()
    # split into the fewest chunks that fit, then balance them: a 320k session becomes
    # two chunks of 160k, not one of 300k and a 20k tail that costs a worker of its own
    total = sum(len(t) for t in turns)
    want = max(1, -(-total // chunk_chars))
    target = total / want
    chunks: list[list[str]] = [[]]
    size = 0
    for t in turns:
        if len(chunks) < want and chunks[-1] and size + len(t) / 2 > target:
            chunks.append([])
            size = 0
        chunks[-1].append(t)
        size += len(t)
    paths, counts = [], []
    n = len(chunks)
    for i, ch in enumerate(chunks, 1):
        p = out / f"chunk-{i:02d}.txt"
        body = rest + "\n".join(ch) + "\n" + end_marker(i, n) + "\n"
        lines = (top + body).count("\n")
        write_text(p, top.replace("{CHUNK}", f"{i} of {n}").replace("{LINES}", str(lines)) + body)
        paths.append(p)
        counts.append(lines)
    return paths, counts


def memory_index_text(cfg: dict, store: str) -> str:
    idx = cfg["projects_root"] / store / "memory" / "MEMORY.md"
    if idx.exists():
        return read_text(idx)
    return "(this project has no MEMORY.md index yet)"


def do_extract(jsonl: Path, out: Path, cfg: dict, store: str | None = None) -> dict:
    turns, meta = extract_session(jsonl, cfg, store)
    sub, agents = subagent_errors(jsonl, cfg)
    meta["subagents_scanned"] = agents
    meta["subagent_errors"] = len(sub)
    meta["empty"] = not turns and not sub   # bookkeeping entries only: nothing for a worker to read
    if sub:
        turns.append("SUBAGENT TOOL ERRORS (" + str(agents) + " subagent transcripts scanned, errors only):\n"
                     + "\n".join("- " + s for s in sub) + "\n")
    top = ("SESSION DIGEST INPUT: chunk {CHUNK}. This file has {LINES} lines and its last line reads "
           "\"END OF CHUNK {CHUNK}\"; if you have not seen that line you have not read the whole file.\n")
    rest = (
        f"session: {meta['session']}\nstore: {meta['store']}\nproject_path: {meta['cwd']}\n"
        f"git_branch: {meta['git_branch']}\nclaude_code_version: {meta['version']}\n"
        f"started: {meta['first']}\nended: {meta['last']}\n"
        f"human_turns: {meta['human_turns']} (of which slash commands: {meta['slash_commands']}, "
        f"typed while Claude was working: {meta['queued_turns']})  "
        f"question_answers: {meta['question_answers']}  tool_rejections: {meta['tool_rejections']}\n"
        f"assistant_turns: {meta['assistant_turns']}  tool_calls: {meta['tool_calls']}  "
        f"tool_errors: {meta['tool_errors']}  compactions: {meta['compactions']}\n"
        "\nCURRENT MEMORY INDEX FOR THIS PROJECT (MEMORY.md, for spotting contradictions):\n"
        + memory_index_text(cfg, meta["store"]).strip() + "\n\n"
        "TRANSCRIPT. Every line that starts with USER is the user's own words or action and is the "
        "primary signal: USER (typed at the prompt), USER (slash command), USER (typed while Claude "
        "was working), USER (answered Claude's question), USER REJECTED TOOL CALL. CLAUDE turns are "
        "capped. TOOL lines are calls; TOOL ERROR lines are failed results.\n\n"
    )
    paths, counts = write_chunks(turns, top, rest, out, cfg["chunk_chars"])
    meta["chunks"] = [str(p) for p in paths]
    meta["chunk_lines"] = counts
    meta["chars"] = sum(len(t) for t in turns)
    write_text(out / "meta.json", json.dumps(meta, indent=2))
    return meta


def cmd_extract(args, cfg):
    meta = do_extract(Path(args.session), Path(args.out), cfg)
    print(json.dumps({k: v for k, v in meta.items() if k != "chunks"}, indent=2))
    print("chunks:", len(meta["chunks"]))


def cmd_extract_all(args, cfg):
    pending = json.loads(read_text(Path(args.pending)))
    run = Path(args.run).resolve()
    manifest = {"run": str(run), "created": now_iso(), "digest_version": cfg["digest_version"],
                "prompt": str(HOME / "prompts" / "digest.md"), "sessions": []}
    for i, s in enumerate(pending, 1):
        out = run / "extract" / s["store"] / s["session"]
        meta = do_extract(Path(s["path"]), out, cfg, s["store"])
        manifest["sessions"].append({
            "store": s["store"], "session": s["session"], "reason": s["reason"],
            "project_path": meta["cwd"], "chunks": meta["chunks"], "chars": meta["chars"],
            "extract_dir": str(out), "meta": str(out / "meta.json"),
            "digest_path": str(digest_path(s["store"], s["session"])),
            "source_size": meta["source_size"], "source_mtime": meta["source_mtime"],
        })
        print(f"[{i}/{len(pending)}] {s['store']} {s['session'][:8]} -> {len(meta['chunks'])} chunk(s), "
              f"{meta['chars']/1e3:.0f}k chars", file=sys.stderr)
    mp = run / "extract-manifest.json"
    write_text(mp, json.dumps(manifest, indent=2))
    print(str(mp))


# ----------------------------------------------------------------------------
# digest batches: prepare (extract + briefs) and finish (validate + stamp + move)
# ----------------------------------------------------------------------------

DIGEST_SECTIONS = ("Summary", "Corrections from the user", "Instructions the user repeated or emphasized",
                   "Tool and environment failures", "Decisions and state changes",
                   "Contradictions with the current memory index", "Candidate memories")


def load_state() -> dict:
    p = ROOT / "state.json"
    if p.exists():
        try:
            return json.loads(read_text(p))
        except json.JSONDecodeError:
            pass
    return {"last_digest": None, "last_dream": {}}


def save_state(state: dict) -> None:
    write_text(ROOT / "state.json", json.dumps(state, indent=2) + "\n")


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            nl = text.find("\n", end + 1)
            return text[nl + 1:] if nl >= 0 else ""
    return text


def digest_frontmatter(meta: dict, cfg: dict, chunks: int, digested_at: str, by: str) -> str:
    """The watermark. Written by this script, never by a model: `pending` trusts these fields."""
    return ("---\n"
            f"session: {meta['session']}\nstore: {meta['store']}\n"
            f"project_path: {meta.get('cwd') or 'null'}\n"
            f"started: {meta.get('first') or 'null'}\nended: {meta.get('last') or 'null'}\n"
            f"source_size: {meta['source_size']}\nsource_mtime: {meta['source_mtime']}\n"
            f"chunks: {chunks}\nhuman_turns: {meta['human_turns']}\n"
            f"question_answers: {meta.get('question_answers', 0)}\ntool_rejections: {meta.get('tool_rejections', 0)}\n"
            f"tool_errors: {meta['tool_errors']}\ndigest_version: {cfg['digest_version']}\n"
            f"digested_at: {digested_at}\nwritten_by: {by}\n"
            "---\n")


def section_items(body: str, section: str) -> int:
    """Bullets under `## <section>`, not counting `- none` (workers sometimes add a remark after it)."""
    m = re.search(rf"^## {re.escape(section)}\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    if not m:
        return 0
    items = [l for l in m.group(1).splitlines() if l.startswith("- ")]
    return len([l for l in items if not re.match(r"- none\b", l.strip(), re.I)])


def clean_digest_body(raw: str) -> tuple[str | None, str]:
    """(body, problem). The body starts at `## Summary`; anything a worker put before it is dropped."""
    body = strip_frontmatter(raw).strip()
    if body.startswith("```"):   # a worker wrapped the whole file in a code fence
        body = re.sub(r"\n```\s*$", "", re.sub(r"^```[a-z]*\s*\n", "", body)).strip()
        body = strip_frontmatter(body).strip()
    m = re.search(r"^## Summary\s*$", body, re.M)
    if not m:
        return None, "no '## Summary' section"
    body = body[m.start():]
    missing = [s for s in DIGEST_SECTIONS if not re.search(rf"^## {re.escape(s)}\s*$", body, re.M)]
    if len(missing) > 3:
        return None, "missing sections: " + ", ".join(missing)
    return body, ("missing sections: " + ", ".join(missing)) if missing else ""


def stub_digest_body(meta: dict) -> str:
    lines = ["## Summary",
             f"Empty session: the transcript ({meta['source_size']} bytes) holds bookkeeping entries only, "
             "with no user turns, no assistant text and no tool calls. Stub written by dreamctl; "
             "no worker read it.", ""]
    for s in DIGEST_SECTIONS[1:]:
        lines += [f"## {s}", "- none", ""]
    return "\n".join(lines)


def digest_brief(kind: str, s: dict, k: int = 0) -> str:
    """The whole assignment for one worker. Paths are absolute; the worker retypes nothing."""
    n = len(s["chunks"])
    prompt = posix(HOME / "prompts" / "digest.md")
    head = f"# Digest job {s['index']:03d}: {s['store']} / session {s['session']}\n\n"
    tail = ("\nReturn the JSON summary the instructions describe, with `path` set to the file you wrote. "
            "If you could not read an input or write the output, return `\"ok\": false` and say why in `note`.\n")
    if kind == "single":
        return (head + f"1. Read the digest instructions in full: {prompt}\n"
                f"2. Read the transcript chunk in full: {posix(s['chunks'][0])}\n"
                f"   It has {s['chunk_lines'][0]} lines and its last line reads `{end_marker(1, 1)}`. One Read call may "
                "return only part of it; keep reading with `offset` until you have seen that line.\n"
                f"3. Write the digest to exactly this path, and write no other file: {posix(s['staged'])}\n" + tail)
    if kind == "partial":
        return (head + f"This session is long and was split into {n} chunks. You have chunk {k} of {n}; other workers "
                "have the rest, and a merge step combines the partial digests.\n\n"
                f"1. Read the digest instructions in full (see \"Multi-chunk sessions\"): {prompt}\n"
                f"2. Read your chunk in full: {posix(s['chunks'][k - 1])}\n"
                f"   It has {s['chunk_lines'][k - 1]} lines and its last line reads `{end_marker(k, n)}`. One Read call may "
                "return only part of it; keep reading with `offset` until you have seen that line.\n"
                f"3. Write a partial digest (the same section shape as a full digest, covering only your chunk) to "
                f"exactly this path, and write no other file: {posix(s['partials'][k - 1])}\n" + tail)
    return (head + f"This session was split into {n} chunks and one worker per chunk wrote a partial digest. "
            "Merge them into the final digest.\n\n"
            f"1. Read the digest instructions in full (see \"Multi-chunk sessions\"): {prompt}\n"
            "2. Read every partial digest that exists, in order (a missing file means that chunk's worker "
            "failed; say so in the Summary and in `note`):\n"
            + "".join(f"   - {posix(p)}\n" for p in s["partials"])
            + f"3. Write the final digest to exactly this path, and write no other file: {posix(s['staged'])}\n" + tail)


def worker_opts(cfg: dict, role: str) -> dict:
    """Which subagent does a role (`digest`, `dream`, `global`): the `workers` block of
    config.json, passed to the workflow as agent options. `{"model": "sonnet"}` picks a
    model, `{"agentType": "my-agent"}` a named agent definition, `{}` the session model."""
    opts = (cfg.get("workers") or {}).get(role) or {}
    return {k: v for k, v in opts.items() if k in ("model", "agentType", "effort") and v}


def commit_hint(paths: list[str], message: str) -> list[str] | None:
    """Two git commands for the caller to run when this checkout tracks its data folders.

    A checkout that git-ignores them (the default for a public clone) gets None."""
    import subprocess
    probe = paths[0] if (ROOT / paths[0]).is_file() else paths[0].rstrip("/") + "/"   # `dir/` matches before the folder exists
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", probe],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 1:   # 0 = ignored, 128 = not a git repository
        return None
    return ["git add " + " ".join(paths), f'git commit -q -m "{message}"']


def seal_run(run: Path, cfg: dict) -> dict:
    """Validate, stamp and move every staged digest of one run. Safe to run twice."""
    manifest = json.loads(read_text(run / "digest-manifest.json"))
    sealed, missing, malformed, notes = [], [], [], []
    totals = {"corrections": 0, "failures": 0, "candidates": 0}
    for s in manifest["sessions"]:
        staged, final = Path(s["staged"]), Path(s["digest_path"])
        tag = f"{s['store']} {s['session'][:8]}"
        meta = json.loads(read_text(Path(s["meta"])))
        if not staged.exists():
            fm = parse_frontmatter(read_text(final)) if final.exists() else {}
            if fm.get("source_size") == str(meta["source_size"]) and fm.get("digest_version") == str(cfg["digest_version"]):
                sealed.append(tag)   # sealed by an earlier call
            else:
                missing.append(tag)
            continue
        body, problem = clean_digest_body(read_text(staged))
        if body is None:
            malformed.append(f"{tag}: {problem}")
            continue
        if problem:
            notes.append(f"{tag}: {problem}")
        by = "dreamctl (empty-session stub)" if s.get("stub") else "digest worker; frontmatter stamped by dreamctl"
        write_text(final, digest_frontmatter(meta, cfg, len(s["chunks"]), iso_of_mtime(staged), by) + "\n" + body.rstrip() + "\n")
        staged.replace(staged.with_suffix(".sealed.md"))
        sealed.append(tag)
        totals["corrections"] += section_items(body, "Corrections from the user")
        totals["failures"] += section_items(body, "Tool and environment failures")
        totals["candidates"] += section_items(body, "Candidate memories")
    out = {"run": posix(run), "sessions": len(manifest["sessions"]), "sealed": len(sealed), "missing": missing,
           "malformed": malformed, "notes": notes, "totals": totals, "finished_at": now_iso()}
    write_text(run / "digest-finished.json", json.dumps(out, indent=2))
    return out


def seal_leftovers(cfg: dict) -> int:
    """Seal digests that a killed or abandoned run left staged, so they are not digested twice."""
    n = 0
    runs = ROOT / "runs"
    if not runs.is_dir():
        return 0
    for mp in sorted(runs.glob("*/digest-manifest.json")):
        if (mp.parent / "digest-finished.json").exists():
            continue
        try:
            n += seal_run(mp.parent, cfg)["sealed"]
        except (OSError, json.JSONDecodeError, KeyError) as ex:
            print(f"could not seal leftover run {mp.parent.name}: {ex}", file=sys.stderr)
    return n


def cmd_digest_prepare(args, cfg):
    leftovers = seal_leftovers(cfg)
    only = [x.strip() for x in (args.sessions or "").split(",") if x.strip()] or None
    # sessions named with --sessions are digested even if still open; otherwise active ones wait
    everything = pending_sessions(cfg, args.project, only, include_active=True)
    pending = [r for r in everything if only or not r["active"]]
    summary = {"scope": args.project, "pending_sessions": len(pending),
               "pending_mb": round(sum(r["size"] for r in pending) / 1e6, 1), "sealed_from_earlier_runs": leftovers,
               "skipped_still_active": [f"{r['store']} {r['session'][:8]}" for r in everything if r not in pending]}
    if not pending:
        summary["message"] = ("nothing to digest now: the remaining sessions are still being written to; run /digest again later"
                              if summary["skipped_still_active"] else "nothing pending: every session has a current digest")
        print(json.dumps(summary, indent=2))
        return
    budget = args.max_agents or cfg.get("digest_batch_agents", 45)
    run = new_run_dir("digest")
    labels = store_labels(list_projects(cfg))
    sessions, jobs, agents, stubs, taken = [], [], 0, 0, 0
    for r in pending:
        out = run / "extract" / r["store"] / r["session"]
        meta = do_extract(Path(r["path"]), out, cfg, r["store"])
        n = len(meta["chunks"])
        cost = 0 if meta["empty"] else n + (1 if n > 1 else 0)
        if jobs and agents + cost > budget:
            shutil.rmtree(out, ignore_errors=True)   # next batch extracts it again
            break
        taken += 1
        s = {"store": r["store"], "session": r["session"], "reason": r["reason"], "project_path": meta["cwd"],
             "chunks": meta["chunks"], "chunk_lines": meta["chunk_lines"], "chars": meta["chars"],
             "extract_dir": str(out), "meta": str(out / "meta.json"),
             "staged": str(run / "out" / r["store"] / f"{r['session']}.md"),
             "digest_path": str(digest_path(r["store"], r["session"])),
             "source_size": meta["source_size"], "source_mtime": meta["source_mtime"],
             "partials": [str(out / f"partial-{k:02d}.md") for k in range(1, n + 1)] if n > 1 else [],
             "stub": bool(meta["empty"]), "index": None}
        if meta["empty"]:
            write_text(Path(s["staged"]), stub_digest_body(meta))
            stubs += 1
        else:
            s["index"] = len(jobs) + 1
            tag = f"{s['index']:03d}"
            if n == 1:
                write_text(run / "jobs" / f"{tag}.md", digest_brief("single", s))
            else:
                for k in range(1, n + 1):
                    write_text(run / "jobs" / f"{tag}-c{k:02d}.md", digest_brief("partial", s, k))
                write_text(run / "jobs" / f"{tag}-merge.md", digest_brief("merge", s))
            jobs.append([f"{labels.get(r['store'], r['store'])[:24]}/{r['session'][:8]}", n])
            agents += cost
        sessions.append(s)
        print(f"[{taken}/{len(pending)}] {r['store']} {r['session'][:8]} -> "
              + ("empty, stub written" if meta["empty"] else f"{n} chunk(s), {meta['chars']/1e3:.0f}k chars"),
              file=sys.stderr)
    write_text(run / "digest-manifest.json", json.dumps({
        "run": str(run), "scope": args.project, "created": now_iso(), "digest_version": cfg["digest_version"],
        "prompt": str(HOME / "prompts" / "digest.md"), "sessions": sessions}, indent=2))
    write_text(run / "workflow-args.json", json.dumps(
        {"run": posix(run), "worker": worker_opts(cfg, "digest"), "jobs": jobs}))
    summary.update({"run": posix(run), "batch_sessions": taken, "batch_agents": agents, "batch_stubs": stubs,
                    "left_for_next_batch": len(pending) - taken,
                    "workflow_script": posix(HOME / "workflows" / "digest.js"),
                    "workflow_args_file": posix(run / "workflow-args.json")})
    if not jobs:
        summary["message"] = "this batch holds empty sessions only: skip the workflow and run digest-finish"
    print(json.dumps(summary, indent=2))


def cmd_digest_finish(args, cfg):
    run = Path(args.run).resolve()
    out = seal_run(run, cfg)
    scope = json.loads(read_text(run / "digest-manifest.json")).get("scope", "all")
    still = pending_sessions(cfg, scope)
    out["still_pending_in_scope"] = len(still)
    state = load_state()
    state["last_digest"] = now_iso()
    save_state(state)
    out["commit"] = commit_hint(["digests", "state.json"], f"digest: {out['sealed']} sessions ({scope})") if out["sealed"] else None
    print(json.dumps(out, indent=2))
    print(f"sealed {out['sealed']} of {out['sessions']} digests; {len(out['missing'])} missing, "
          f"{len(out['malformed'])} malformed; {len(still)} sessions still pending in scope {scope}", file=sys.stderr)


# ----------------------------------------------------------------------------
# dream packets
# ----------------------------------------------------------------------------


def store_snapshot(mem_dir: Path) -> dict:
    files = {}
    if mem_dir.is_dir():
        for p in sorted(mem_dir.glob("*.md")):
            files[p.name] = {"sha256": sha256_file(p), "size": p.stat().st_size,
                             "mtime": iso_of_mtime(p), "content": read_text(p)}
    return files


def git_facts(project_path: str | None) -> str:
    if not project_path or not Path(project_path).is_dir():
        return "(project path unknown or missing)"
    import subprocess
    kw = dict(capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    try:
        top = subprocess.run(["git", "-C", project_path, "rev-parse", "--show-toplevel"], **kw)
        if top.returncode != 0:
            return "(not a git repository)"
        log = subprocess.run(["git", "-C", project_path, "log", "--date=short",
                              "--format=%ad %h %s", "-40"], **kw).stdout
        st = subprocess.run(["git", "-C", project_path, "status", "--short"], **kw).stdout
        br = subprocess.run(["git", "-C", project_path, "branch", "--show-current"], **kw).stdout.strip()
        return f"branch: {br}\nrecent commits:\n{log}\nworking tree:\n{st or '(clean)'}"
    except (OSError, subprocess.SubprocessError) as ex:
        return f"(git check failed: {ex})"


def digests_for(store: str) -> list[Path]:
    d = ROOT / "digests" / store
    return sorted(d.glob("*.md")) if d.is_dir() else []


def candidate_sections(store: str) -> str:
    """Pull the `## Candidate memories` section out of each digest (for the global pass)."""
    out = []
    for p in digests_for(store):
        txt = read_text(p)
        m = re.search(r"^## Candidate memories\s*\n(.*?)(?=^## |\Z)", txt, re.S | re.M)
        if m and m.group(1).strip():
            out.append(f"### {store} / session {p.stem[:8]}\n{m.group(1).strip()}\n")
    return "\n".join(out)


def write_project_packet(pr: dict, run: Path, cfg: dict) -> dict:
    store = pr["store"]
    mem_dir = Path(pr["memory_dir"])
    snap = store_snapshot(mem_dir)
    digs = digests_for(store)
    pk_dir = run / "packets" / store
    parts = [f"# DREAM PACKET: {store}\n",
             f"kind: project\nmemory_dir: {mem_dir}\nproject_path: {pr['project_path']}\n"
             f"memory_files: {len(snap)}\ndigests: {len(digs)}\ntranscripts_on_disk: {pr['transcripts']}\n",
             "\n## Project facts (read-only git check)\n", git_facts(pr["project_path"]),
             "\n\n## Current memory store\n"]
    if not snap:
        parts.append("(empty: this project has no memory files yet)\n")
    for name, info in snap.items():
        parts.append(f"\n### FILE {name}  (sha256 {info['sha256'][:12]}, {info['size']} bytes, modified {info['mtime']})\n"
                     f"```markdown\n{info['content'].rstrip()}\n```\n")
    parts.append("\n## Session digests\n")
    if not digs:
        parts.append("(no digests: no transcripts survive for this project; do Verify and Organize only)\n")
    for p in digs:
        parts.append(f"\n### DIGEST {p.stem}\n{read_text(p).rstrip()}\n")
    lines, chars = write_packet(pk_dir / "packet.md", "".join(parts), store)
    write_text(pk_dir / "packet.json", json.dumps({
        "store": store, "kind": "project", "memory_dir": str(mem_dir), "project_path": pr["project_path"],
        "files": {k: {"sha256": v["sha256"], "size": v["size"], "mtime": v["mtime"]} for k, v in snap.items()},
        "digests": [p.stem for p in digs], "bundled_at": now_iso(),
    }, indent=2))
    return {"store": store, "kind": "project", "packet": str(pk_dir / "packet.md"),
            "packet_json": str(pk_dir / "packet.json"), "proposals_out": str(run / f"proposals-{store}.json"),
            "project_path": pr["project_path"], "memory_files": len(snap), "digests": len(digs),
            "packet_lines": lines, "packet_chars": chars}


def packet_end(store: str) -> str:
    return f"END OF PACKET {store}"


def write_packet(path: Path, text: str, store: str) -> tuple[int, int]:
    """Write a packet with an end marker as its last line; return (lines, chars)."""
    body = text.rstrip("\n") + "\n\n" + packet_end(store) + "\n"
    write_text(path, body)
    return body.count("\n"), len(body)


def write_global_packet(run: Path, cfg: dict, stores: list[str]) -> dict:
    g: Path = cfg["global_store"]
    snap = store_snapshot(g)
    claude_md = cfg["user_claude_md"]
    claude_md_text = read_text(claude_md) if claude_md.exists() else "(missing)"
    pk_dir = run / "packets" / "global"
    parts = [f"# DREAM PACKET: global\n",
             f"kind: global\nmemory_dir: {g}\nindex_file: {claude_md}\nmemory_files: {len(snap)}\n",
             f"\n## User-level CLAUDE.md (the `@` import lines under `{cfg['global_index_heading']}` are the global index; "
             f"a file `x.md` in the store is imported as `{cfg['global_import_prefix']}x.md`)\n",
             f"```markdown\n{claude_md_text.rstrip()}\n```\n", "\n## Current global memory store\n"]
    for name, info in snap.items():
        parts.append(f"\n### FILE {name}  (sha256 {info['sha256'][:12]}, {info['size']} bytes, modified {info['mtime']})\n"
                     f"```markdown\n{info['content'].rstrip()}\n```\n")
    parts.append("\n## Candidate memories flagged across every project's digests\n")
    any_c = False
    for s in stores:
        c = candidate_sections(s)
        if c:
            any_c = True
            parts.append(c + "\n")
    if not any_c:
        parts.append("(none flagged yet)\n")
    parts.append("\n## Per-project proposals from this run\n"
                 "Your brief lists the `proposals-<store>.json` files written in this run. "
                 "Proposals marked `\"promote\": true` are the per-project dreamers' nominations for global.\n")
    lines, chars = write_packet(pk_dir / "packet.md", "".join(parts), "global")
    write_text(pk_dir / "packet.json", json.dumps({
        "store": "global", "kind": "global", "memory_dir": str(g), "index_file": str(claude_md),
        "index_sha256": sha256_text(claude_md_text),
        "files": {k: {"sha256": v["sha256"], "size": v["size"], "mtime": v["mtime"]} for k, v in snap.items()},
        "bundled_at": now_iso(),
    }, indent=2))
    return {"store": "global", "kind": "global", "packet": str(pk_dir / "packet.md"),
            "packet_json": str(pk_dir / "packet.json"), "proposals_out": str(run / "proposals-global.json"),
            "memory_files": len(snap), "packet_lines": lines, "packet_chars": chars}


def parse_scope(scope: str, cfg: dict) -> tuple[list[dict], bool]:
    """(project rows, include_global). Scope: all | global | STORE[,STORE...][,global]."""
    rows = list_projects(cfg)
    has_global = cfg["global_store"] is not None
    if scope == "all":
        return [p for p in rows if not p["excluded"]], has_global
    names = [x.strip() for x in scope.split(",") if x.strip()]
    want_global = "global" in names
    if want_global and not has_global:
        sys.exit("the global store is disabled: set global_store and user_claude_md in config.json to use it")
    by = {p["store"]: p for p in rows}
    unknown = [n for n in names if n != "global" and n not in by]
    if unknown:
        sys.exit("no such project store: " + ", ".join(unknown) + "  (see `dreamctl.py projects`)")
    return [by[n] for n in names if n != "global"], want_global


def bundle(scope: str, run: Path, cfg: dict) -> dict:
    projects, want_global = parse_scope(scope, cfg)
    entries = []
    for p in projects:
        if not p["has_store"] and p["digests"] == 0:
            continue  # nothing to dream about
        entries.append(write_project_packet(p, run, cfg))
    # the global pass reads candidates from every digested project, not only the ones dreamed this run
    everyone = [p["store"] for p in list_projects(cfg) if not p["excluded"]]
    manifest = {
        "run": str(run), "scope": scope, "created": now_iso(),
        "prompts": {"project": str(HOME / "prompts" / "dream-project.md"),
                    "global": str(HOME / "prompts" / "dream-global.md")},
        "projects": entries,
        "global": write_global_packet(run, cfg, everyone) if want_global else None,
    }
    write_text(run / "dream-manifest.json", json.dumps(manifest, indent=2))
    return manifest


def cmd_bundle(args, cfg):
    run = Path(args.run).resolve()
    run.mkdir(parents=True, exist_ok=True)
    manifest = bundle(args.scope, run, cfg)
    for e in manifest["projects"]:
        print(f"packet {e['store']}: {e['memory_files']} memory files, {e['digests']} digests", file=sys.stderr)
    print(str(run / "dream-manifest.json"))


def dream_brief(e: dict, manifest: dict) -> str:
    """The whole assignment for one dreamer. Paths are absolute; the worker retypes nothing."""
    read = (f"   It has {e['packet_lines']} lines and its last line reads `{packet_end(e['store'])}`. One Read call "
            "may return only part of it; keep reading with `offset` until you have seen that line.\n")
    tail = ("\nReturn the JSON summary the instructions describe. If you could not read an input or write the "
            "output, say so in `note` and report `\"proposals\": 0`.\n")
    if e["kind"] == "global":
        files = [p["proposals_out"] for p in manifest["projects"]]
        return ("# Dream job: the global memory store\n\n"
                f"1. Read the instructions in full: {posix(manifest['prompts']['global'])}\n"
                f"2. Read the global packet in full: {posix(e['packet'])}\n" + read
                + "3. Read each per-project proposals file written in this run (a missing file means that "
                "store's dreamer failed; skip it and say so in `note`):\n"
                + ("".join(f"   - {posix(f)}\n" for f in files) if files else "   - (none: no project store was dreamed in this run)\n")
                + f"4. Write the JSON array of global proposals to exactly this path, and write no other file: "
                f"{posix(e['proposals_out'])}\n   Write `[]` if nothing qualifies.\n" + tail)
    pp = e.get("project_path")
    return (f"# Dream job: project memory store {e['store']}\n\n"
            f"1. Read the instructions in full: {posix(manifest['prompts']['project'])}\n"
            f"2. Read the packet in full (the current memory files, then every session digest): {posix(e['packet'])}\n" + read
            + ("3. You may verify claims with read-only commands in the project directory "
               f"`{pp}`: `git -C <dir> log`, `git -C <dir> show`, `ls`, `test -e`. Never run anything that writes "
               "there, and never write to the memory store.\n" if pp else
               "3. This store has no known project directory; verify from the packet alone.\n")
            + f"4. Write the JSON array of proposals to exactly this path, and write no other file: "
            f"{posix(e['proposals_out'])}\n   Every proposal's `store` is `{e['store']}`. Write `[]` if the store needs nothing.\n"
            + tail)


def cmd_dream_prepare(args, cfg):
    run = new_run_dir("dream")
    manifest = bundle(args.scope, run, cfg)
    names = store_labels(list_projects(cfg))
    labels = []
    for i, e in enumerate(manifest["projects"], 1):
        write_text(run / "jobs" / f"dream-{i:03d}.md", dream_brief(e, manifest))
        labels.append(names.get(e["store"], e["store"])[:28])
    if manifest["global"]:
        write_text(run / "jobs" / "dream-global.md", dream_brief(manifest["global"], manifest))
    write_text(run / "workflow-args.json", json.dumps(
        {"run": posix(run), "workers": {"dream": worker_opts(cfg, "dream"), "global": worker_opts(cfg, "global")},
         "projects": labels, "global": bool(manifest["global"])}))
    undigested = len(pending_sessions(cfg, "all"))
    big = cfg.get("packet_warn_chars", 600000)
    summary = {
        "scope": args.scope, "run": posix(run),
        "project_stores": len(manifest["projects"]), "global_pass": bool(manifest["global"]),
        "agents": len(manifest["projects"]) + (1 if manifest["global"] else 0),
        "stores": [{"store": e["store"], "memory_files": e["memory_files"], "digests": e["digests"],
                    "packet_chars": e["packet_chars"]} for e in manifest["projects"]],
        "large_packets": [e["store"] for e in manifest["projects"] if e["packet_chars"] > big],
        "sessions_without_a_current_digest": undigested,
        "workflow_script": posix(HOME / "workflows" / "dream.js"),
        "workflow_args_file": posix(run / "workflow-args.json"),
    }
    if not manifest["projects"] and not manifest["global"]:
        summary["message"] = "nothing to dream about in this scope: no memory files and no digests"
    print(json.dumps(summary, indent=2))


# ----------------------------------------------------------------------------
# assemble proposals -> report
# ----------------------------------------------------------------------------

REQUIRED = ("store", "action", "file", "title", "summary", "rationale", "verb", "confidence")
ACTIONS = ("add", "update", "delete")


def validate_proposal(p: dict, i: int) -> list[str]:
    errs = []
    for k in REQUIRED:
        if not p.get(k):
            errs.append(f"missing {k}")
    if p.get("action") not in ACTIONS:
        errs.append(f"bad action {p.get('action')!r}")
    if p.get("action") in ("add", "update") and not p.get("proposed"):
        errs.append("add/update needs proposed content")
    if not re.match(r"^[A-Za-z0-9][\w.-]*\.md$", str(p.get("file", ""))):
        errs.append(f"file name must be a bare .md name, got {p.get('file')!r}")
    return errs


def cmd_assemble(args, cfg):
    run = Path(args.run).resolve()
    manifest = json.loads(read_text(run / "dream-manifest.json"))
    stores: dict[str, dict] = {}
    for e in manifest["projects"]:
        stores[e["store"]] = json.loads(read_text(Path(e["packet_json"])))
    if manifest.get("global"):
        stores["global"] = json.loads(read_text(Path(manifest["global"]["packet_json"])))
    proposals, problems = [], []
    n = 0
    # one proposals file per store in the manifest; a dreamer may only propose for its own store
    expected = [(e["store"], Path(e["proposals_out"])) for e in manifest["projects"]]
    if manifest.get("global"):
        expected.append(("global", Path(manifest["global"]["proposals_out"])))
    claimed: dict[tuple[str, str], str] = {}
    for owner, pf in expected:
        if not pf.exists():
            problems.append(f"{pf.name}: missing; the {owner} dreamer wrote nothing (re-run /dream {owner})")
            continue
        try:
            items = json.loads(read_text(pf))
        except json.JSONDecodeError as ex:
            problems.append(f"{pf.name}: invalid JSON ({ex})")
            continue
        if isinstance(items, dict) and "proposals" in items:
            items = items["proposals"]
        if not isinstance(items, list):
            problems.append(f"{pf.name}: expected a JSON array")
            continue
        for i, p in enumerate(items):
            if not isinstance(p, dict):
                problems.append(f"{pf.name}[{i}]: not a JSON object; dropped")
                continue
            errs = validate_proposal(p, i)
            st = stores.get(p.get("store"))
            if st is None:
                errs.append(f"unknown store {p.get('store')!r}")
            elif p.get("store") != owner:
                errs.append(f"the {owner} dreamer may not propose changes to {p.get('store')}")
            if errs:
                problems.append(f"{pf.name}[{i}] {p.get('title','?')}: " + "; ".join(errs))
                continue
            current = st["files"].get(p["file"])
            if p["action"] in ("update", "delete") and current is None:
                problems.append(f"{pf.name}[{i}] {p['title']}: {p['action']} targets {p['file']} which does not exist; dropped")
                continue
            n += 1
            p["id"] = f"p{n:03d}"
            p["kind"] = st["kind"]
            p["base_hash"] = current["sha256"] if current else None
            first = claimed.setdefault((p["store"], p["file"]), p["id"])
            if first != p["id"]:
                p["note"] = (p.get("note") or "") + f" ({first} targets the same file; accept only one of them)"
            if p["action"] == "add" and current is not None:
                p["action"] = "update"
                p["note"] = (p.get("note") or "") + " (file already existed; treated as update)"
            mem_dir = Path(st["memory_dir"])
            if p["action"] != "add" and (mem_dir / p["file"]).exists():
                p["current"] = read_text(mem_dir / p["file"])
                if sha256_file(mem_dir / p["file"]) != p["base_hash"]:
                    p["note"] = (p.get("note") or "") + " (file changed between bundle and assemble)"
            else:
                p["current"] = None
            p.setdefault("evidence", [])
            p.setdefault("prevalence", {})
            p["source_file"] = pf.name
            proposals.append(p)
    date = args.date or today()
    rep = ROOT / "reports" / date
    k = 2
    while rep.exists() and any(rep.iterdir()):
        rep = ROOT / "reports" / f"{date}-{k}"
        k += 1
    rep.mkdir(parents=True, exist_ok=True)
    names = store_labels(list_projects(cfg))
    for p in proposals:
        if p["kind"] == "global" and p["action"] != "delete":
            # the import line is mechanical: the tooling writes it, whatever the worker put
            p["import_line"] = cfg["global_import_prefix"] + p["file"]
    report = {"run_id": run.name, "run_dir": str(run), "scope": manifest["scope"], "created": now_iso(),
              "stores": {k: {"kind": v["kind"], "label": names.get(k, k), "memory_dir": v["memory_dir"],
                             "index_file": v.get("index_file"),
                             "files": {f: i["sha256"] for f, i in v["files"].items()}} for k, v in stores.items()},
              "proposals": proposals, "problems": problems}
    write_text(rep / "proposals.json", json.dumps(report, indent=2, ensure_ascii=False))
    write_text(rep / "REPORT.md", render_report_md(report))
    render_html(rep)
    state = load_state()
    state.setdefault("last_dream", {})[manifest["scope"]] = now_iso()
    save_state(state)
    for pr in problems:
        print("  problem:", pr, file=sys.stderr)
    out = json.loads(report_summary(rep))
    out["commit"] = commit_hint(["reports", "state.json"], f"dream: {manifest['scope']}, {len(proposals)} proposals")
    print(json.dumps(out, indent=2, ensure_ascii=False))


def resolve_report(arg: str | None) -> Path:
    """A report directory. `latest` (or nothing) is the newest report not yet applied."""
    if arg and arg != "latest":
        rep = Path(arg)
        if not rep.is_absolute() and not rep.exists():
            rep = ROOT / arg
        rep = rep.resolve()
        if not (rep / "proposals.json").exists():
            sys.exit(f"no proposals.json in {rep}")
        return rep
    open_reports = [p.parent for p in (ROOT / "reports").glob("*/proposals.json")
                    if not (p.parent / "applied.json").exists()]
    if not open_reports:
        sys.exit("no report is waiting for review: reports/ holds no report that has not been applied (run /dream first)")
    return max(open_reports, key=lambda d: (d / "proposals.json").stat().st_mtime)


def report_summary(rep: Path) -> str:
    r = json.loads(read_text(rep / "proposals.json"))
    d = load_decisions(rep)
    by_store: dict[str, dict[str, int]] = {}
    for p in r["proposals"]:
        c = by_store.setdefault(p["store"], {"add": 0, "update": 0, "delete": 0})
        c[p["action"]] += 1
    dec = {"accept": 0, "reject": 0, "skip": 0}
    for v in d["decisions"].values():
        dec[v["decision"]] = dec.get(v["decision"], 0) + 1
    out = {"report": posix(rep), "scope": r["scope"], "created": r["created"], "proposals": len(r["proposals"]),
           "by_store": by_store, "high_confidence": [f"{p['id']} {p['store']}/{p['file']}: {p['title']}"
                                                     for p in r["proposals"] if p.get("confidence") == "high"],
           "problems": r["problems"], "decisions": dec,
           "undecided": len(r["proposals"]) - len(d["decisions"]), "review_finished": bool(d.get("finished")),
           "applied": (rep / "applied.json").exists()}
    return json.dumps(out, indent=2, ensure_ascii=False)


def cmd_report(args, cfg):
    print(report_summary(resolve_report(args.report)))


def render_report_md(r: dict) -> str:
    lines = [f"# Dream report {r['run_id']}", "",
             f"scope: {r['scope']}  created: {r['created']}  proposals: {len(r['proposals'])}", ""]
    by: dict[str, list] = {}
    for p in r["proposals"]:
        by.setdefault(p["store"], []).append(p)
    for store, items in by.items():
        lines += [f"## {store}  ({len(items)})", ""]
        for p in items:
            prev = p.get("prevalence") or {}
            pv = f" {prev.get('sessions')}/{prev.get('of')} sessions" if prev.get("sessions") else ""
            lines.append(f"- **{p['id']}** `{p['action']}` `{p['file']}` [{p['verb']}, {p['confidence']}{pv}] "
                         f"{p['title']}: {p['summary']}")
        lines.append("")
    if r["problems"]:
        lines += ["## Problems (dropped proposals)", ""] + [f"- {x}" for x in r["problems"]] + [""]
    lines += [f"Review with `/review` in a Claude Code session started in {HOME} (opens review.html).", ""]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# review page
# ----------------------------------------------------------------------------


def render_html(rep: Path) -> Path:
    tpl = read_text(HOME / "tools" / "review_template.html")
    data = read_text(rep / "proposals.json")
    safe = data.replace("</", "<\\/")
    html = tpl.replace("/*__PROPOSALS__*/null", safe)
    out = rep / "review.html"
    write_text(out, html)
    return out


def cmd_render(args, cfg):
    print(str(render_html(Path(args.report).resolve())))


def load_decisions(rep: Path) -> dict:
    p = rep / "decisions.json"
    if p.exists():
        try:
            return json.loads(read_text(p))
        except json.JSONDecodeError:
            pass
    return {"finished": False, "decisions": {}}


def save_decisions(rep: Path, d: dict) -> None:
    write_text(rep / "decisions.json", json.dumps(d, indent=2, ensure_ascii=False))


class _Review(http.server.BaseHTTPRequestHandler):
    rep: Path = ROOT
    last_activity = dt.datetime.now()
    finished = False
    lock = threading.Lock()

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Review.last_activity = dt.datetime.now()
        path = self.path.split("?")[0]
        if path in ("/", "/review.html"):
            self._send(200, read_text(self.rep / "review.html").encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/proposals.json":
            self._send(200, (self.rep / "proposals.json").read_bytes())
        elif path == "/decisions.json":
            self._send(200, json.dumps(load_decisions(self.rep)).encode("utf-8"))
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        _Review.last_activity = dt.datetime.now()
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"bad json"}')
            return
        with _Review.lock:
            d = load_decisions(self.rep)
            if self.path == "/decision":
                pid = str(body.get("id", ""))
                dec = body.get("decision")
                if not pid or dec not in ("accept", "reject", "skip"):
                    self._send(400, b'{"error":"need id and decision accept|reject|skip"}')
                    return
                entry = {"decision": dec, "at": now_iso()}
                if body.get("edited") is not None:
                    entry["edited"] = body["edited"]
                d["decisions"][pid] = entry
                save_decisions(self.rep, d)
                self._send(200, json.dumps({"ok": True, "count": len(d["decisions"])}).encode())
            elif self.path == "/finish":
                d["finished"] = True
                d["finished_at"] = now_iso()
                save_decisions(self.rep, d)
                _Review.finished = True
                self._send(200, b'{"ok":true}')
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._send(404, b'{"error":"not found"}')


def cmd_serve(args, cfg):
    rep = resolve_report(args.report)
    render_html(rep)   # always from the current template, so template fixes reach old reports
    _Review.rep = rep
    _Review.finished = False
    _Review.last_activity = dt.datetime.now()
    with socket.socket() as s:
        s.bind(("127.0.0.1", args.port))
        port = s.getsockname()[1]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Review)
    url = f"http://127.0.0.1:{port}/"
    idle = dt.timedelta(minutes=args.idle_minutes or cfg.get("review_idle_minutes", 180))

    def watchdog():
        while True:
            threading.Event().wait(15)
            if _Review.finished:
                return
            if dt.datetime.now() - _Review.last_activity > idle:
                print("idle timeout; decisions so far are saved in decisions.json", file=sys.stderr)
                srv.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    print(f"review page: {url}  (report {rep.name}); click Finish when done", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    d = load_decisions(rep)
    n = len(d["decisions"])
    acc = sum(1 for v in d["decisions"].values() if v["decision"] == "accept")
    print(f"{'finished' if d.get('finished') else 'stopped'}: {n} decisions recorded ({acc} accepted)")
    sys.exit(0 if d.get("finished") else 3)


def cmd_decide(args, cfg):
    rep = resolve_report(args.report)
    ids = {p["id"] for p in json.loads(read_text(rep / "proposals.json"))["proposals"]}
    if args.id not in ids:
        sys.exit(f"no proposal {args.id} in {rep.name}")
    d = load_decisions(rep)
    entry = {"decision": args.decision, "at": now_iso()}
    if args.edited_file:
        entry["edited"] = read_text(Path(args.edited_file))
    d["decisions"][args.id] = entry
    if args.finish:
        d["finished"] = True
        d["finished_at"] = now_iso()
    save_decisions(rep, d)
    print(f"{args.id}: {args.decision}")


# ----------------------------------------------------------------------------
# apply
# ----------------------------------------------------------------------------


def _index_matches(line: str, fname: str) -> bool:
    return f"({fname})" in line or f"/{fname})" in line


def _index_line_of(index_text: str, fname: str) -> str | None:
    return next((l for l in index_text.splitlines() if _index_matches(l, fname)), None)


def _index_add_line(index_text: str, line: str) -> str:
    if line.strip() in (l.strip() for l in index_text.splitlines()):
        return index_text
    if not index_text.strip():
        return "# Memory Index\n\n" + line.rstrip() + "\n"
    return index_text.rstrip("\n") + "\n" + line.rstrip() + "\n"


def _index_remove_file(index_text: str, fname: str) -> str:
    keep = [l for l in index_text.splitlines() if not _index_matches(l, fname)]
    return "\n".join(keep).rstrip("\n") + "\n"


def _index_replace_line(index_text: str, fname: str, line: str) -> str:
    """Put `line` where the first index line for the file was; drop any further lines for it."""
    out, done = [], False
    for l in index_text.splitlines():
        if _index_matches(l, fname):
            if not done:
                out.append(line.rstrip())
                done = True
            continue
        out.append(l)
    return "\n".join(out).rstrip("\n") + "\n" if done else _index_add_line(index_text, line)


def default_index_line(p: dict) -> str:
    fm = parse_frontmatter(p.get("proposed") or "")
    hook = fm.get("description") or p.get("summary") or p["title"]
    return f"- [{p['title']}]({p['file']}) — {hook}"


def reconcile_index(mem_dir: Path, pristine: str, proposals: list[dict], applied: dict[str, dict],
                    rewrite_applied: bool) -> list[str]:
    """Make MEMORY.md agree with the files on disk, for the files this report names.

    A dreamer proposes a whole-index rewrite and per-file index lines side by side, and the
    reviewer may accept any subset. Whatever was accepted, each named file ends with exactly
    one index line if it exists and none if it does not. Files the report never mentions are
    left alone (index_check reports them instead).
    """
    idx = mem_dir / "MEMORY.md"
    text = read_text(idx) if idx.exists() else ""
    before = text
    notes = []
    named = []
    for p in proposals:
        if p["file"] != "MEMORY.md" and p["file"] not in named:
            named.append(p["file"])
    for fname in named:
        exists = (mem_dir / fname).exists()
        current = _index_line_of(text, fname)
        ap = applied.get(fname)   # the applied add/update for this file, if any
        if not exists:
            if current is not None:
                text = _index_remove_file(text, fname)
                notes.append(f"removed the index line for {fname} (no such file)")
            continue
        if current is not None:
            if ap and not rewrite_applied:
                line = ap.get("index_line") or current
                if line.strip() != current.strip():
                    text = _index_replace_line(text, fname, line)
                    notes.append(f"replaced the index line for {fname}")
            continue   # an accepted index rewrite already says what it wants for this file
        line = (ap.get("index_line") or default_index_line(ap)) if ap else _index_line_of(pristine, fname)
        if line:
            text = _index_add_line(text, line)
            notes.append(f"{'added' if ap else 'kept'} the index line for {fname}")
    if text != before:
        write_text(idx, text)
    return notes


def index_check(mem_dir: Path) -> dict:
    """Whole-store lint, report only: index lines without a file, files without an index line."""
    idx = mem_dir / "MEMORY.md"
    text = read_text(idx) if idx.exists() else ""
    linked = set(re.findall(r"\]\((?:\./)?([^)\s]+\.md)\)", text))
    files = {p.name for p in mem_dir.glob("*.md")} - {"MEMORY.md"} if mem_dir.is_dir() else set()
    return {"dangling": sorted(f for f in linked if not (mem_dir / f).exists()),
            "unindexed": sorted(files - {Path(f).name for f in linked})}


def _claude_md_add_import(text: str, line: str, heading: str = "## Memories") -> str:
    """Add an `@` import line under `heading` in the user CLAUDE.md, creating the section if needed."""
    if line.strip() in (l.strip() for l in text.splitlines()):
        return text
    lines = text.splitlines()
    i = next((i for i, l in enumerate(lines) if l.strip() == heading), None)
    if i is None:
        return (text.rstrip("\n") + "\n\n" if text.strip() else "") + heading + "\n\n" + line + "\n"
    level = len(heading) - len(heading.lstrip("#"))
    j = i + 1
    while j < len(lines) and not re.match(rf"#{{1,{max(level, 1)}}} ", lines[j]):
        j += 1   # the section runs to the next heading of the same or a higher level
    # insert after the last import line of the section; a section with none gets it at its end
    imports = [n for n in range(i + 1, j) if lines[n].lstrip().startswith("@")]
    if imports:
        k = imports[-1] + 1
    else:
        k = j
        while k > i + 1 and not lines[k - 1].strip():
            k -= 1
    lines.insert(k, line)
    return "\n".join(lines).rstrip("\n") + "\n"


def _claude_md_remove_import(text: str, fname: str) -> str:
    keep = [l for l in text.splitlines() if not (l.strip().startswith("@") and l.strip().endswith("/" + fname))]
    return "\n".join(keep).rstrip("\n") + "\n"


def plan_apply(report: dict, decisions: dict, remap: dict) -> list[dict]:
    """Preflight every accepted proposal against the stores as they are now.

    Shared by --dry-run and the real run, and evaluated before any write, so the write of
    one proposal can never trip the hash guard of another.
    """
    plan, claimed = [], set()
    for p in report["proposals"]:
        if decisions.get(p["id"], {}).get("decision") != "accept":
            continue
        mem_dir = Path(remap.get(p["store"], report["stores"][p["store"]]["memory_dir"]))
        target = mem_dir / p["file"]
        content = decisions[p["id"]].get("edited")
        if content is None:
            content = p.get("proposed")
        skip = None
        if (p["store"], p["file"]) in claimed:
            skip = "another accepted proposal already targets this file"
        elif target.exists():
            if p.get("base_hash") and sha256_file(target) != p["base_hash"]:
                skip = "file changed since the dream (re-run /dream)"
            elif p["action"] == "add":
                skip = "file appeared since the dream"
        elif p["action"] in ("update", "delete"):
            skip = "file no longer exists"
        if not skip and p["action"] in ("add", "update") and not (content or "").strip():
            skip = "no content"
        if not skip:
            claimed.add((p["store"], p["file"]))
        plan.append({"p": p, "mem_dir": mem_dir, "target": target, "content": content, "skip": skip})
    return plan


def cmd_apply(args, cfg):
    rep = resolve_report(args.report)
    report = json.loads(read_text(rep / "proposals.json"))
    decisions = load_decisions(rep)["decisions"]
    remap = dict(kv.split("=", 1) for kv in (args.remap or []))
    user_md = Path(args.user_claude_md) if args.user_claude_md else cfg["user_claude_md"]
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    plan = plan_apply(report, decisions, remap)
    if not plan:
        print("nothing accepted; nothing to apply")
        return
    dirs = {x["p"]["store"]: x["mem_dir"] for x in plan}
    for store, mem_dir in sorted(dirs.items()):
        if (mem_dir / ".consolidate-lock").exists():
            sys.exit(f"refusing: {mem_dir} has a .consolidate-lock (Claude Code auto-dream is running). Retry later.")

    def res_of(x, status):
        p = x["p"]
        return {"id": p["id"], "store": p["store"], "action": p["action"], "file": p["file"],
                "title": p["title"], "status": status}

    results, store_info = [], {}
    if args.dry_run:
        results = [res_of(x, "skipped: " + x["skip"] if x["skip"] else "would " + x["p"]["action"]) for x in plan]
    else:
        live = sorted({x["p"]["store"] for x in plan if not x["skip"]})
        for store in live:   # every snapshot is taken before the first write
            mem_dir = dirs[store]
            snap = ROOT / "snapshots" / stamp / store
            if mem_dir.is_dir():
                shutil.copytree(mem_dir, snap, dirs_exist_ok=True)
            else:
                snap.mkdir(parents=True, exist_ok=True)
            if store == "global" and user_md.exists():
                shutil.copy2(user_md, snap / "CLAUDE.md.user")
            store_info[store] = {"memory_dir": str(mem_dir), "snapshot": str(snap)}
            print(f"snapshot {store} -> {snap}")
        status: dict[str, str] = {x["p"]["id"]: "skipped: " + x["skip"] for x in plan if x["skip"]}
        for store in live:
            mem_dir = dirs[store]
            kind = report["stores"][store]["kind"]
            idx = mem_dir / "MEMORY.md"
            pristine_index = read_text(idx) if idx.exists() else ""
            items = [x for x in plan if x["p"]["store"] == store and not x["skip"]]
            items.sort(key=lambda x: 0 if x["p"]["file"] == "MEMORY.md" else 1)   # the index rewrite goes first
            applied: dict[str, dict] = {}
            rewrite_applied = False
            for x in items:
                p, target = x["p"], x["target"]
                if p["action"] in ("add", "update"):
                    write_text(target, x["content"].rstrip("\n") + "\n")
                    status[p["id"]] = "applied"
                    if p["file"] == "MEMORY.md":
                        rewrite_applied = True
                    else:
                        applied[p["file"]] = p
                    if kind == "global":
                        line = p.get("import_line") or (cfg["global_import_prefix"] or "@~/.claude/memories/") + p["file"]
                        txt = read_text(user_md) if user_md.exists() else ""
                        write_text(user_md, _claude_md_add_import(txt, line, cfg["global_index_heading"]))
                else:   # delete = move to the archive with the reason beside it
                    arch = ROOT / "archive" / stamp / store
                    arch.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(arch / p["file"]))
                    write_text(arch / (p["file"] + ".why.md"),
                               f"# archived {now_iso()}\n\n{p['title']}\n\n{p['rationale']}\n\n"
                               f"Restore by copying the file back to {mem_dir}.\n")
                    status[p["id"]] = f"archived -> {arch / p['file']}"
                    if kind == "global" and user_md.exists():
                        write_text(user_md, _claude_md_remove_import(read_text(user_md), p["file"]))
            if kind == "project":
                mine = [p for p in report["proposals"] if p["store"] == store]
                store_info[store]["index_notes"] = reconcile_index(mem_dir, pristine_index, mine, applied, rewrite_applied)
                store_info[store]["index_check"] = index_check(mem_dir)
        results = [res_of(x, status[x["p"]["id"]]) for x in plan]
    out = {"applied_at": now_iso(), "dry_run": bool(args.dry_run), "snapshot": None if args.dry_run else stamp,
           "results": results, "stores": store_info}
    write_text(rep / ("apply-dryrun.json" if args.dry_run else "applied.json"), json.dumps(out, indent=2, ensure_ascii=False))
    n_applied = sum(1 for r in results if r["status"].startswith(("applied", "archived")))
    hint = None if args.dry_run else commit_hint(["reports", "snapshots", "archive"], f"review: {rep.name}, {n_applied} applied")
    for r in results:
        print(f"{r['id']} {r['action']:<6} {r['store']}/{r['file']}: {r['status']}")
    for store, info in store_info.items():
        for n in info.get("index_notes", []):
            print(f"index {store}: {n}")
        chk = info.get("index_check") or {}
        if chk.get("dangling"):
            print(f"warning {store}: index lines without a file: {', '.join(chk['dangling'])}")
        if chk.get("unindexed"):
            print(f"warning {store}: files without an index line: {', '.join(chk['unindexed'])}")
    done = sum(1 for r in results if r["status"].startswith(("applied", "archived", "would")))
    print(f"{done}/{len(results)} {'planned' if args.dry_run else 'done'}; snapshot {'(dry run)' if args.dry_run else stamp}")
    if hint:
        print("to commit: " + " ; ".join(hint))


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------


_VALUE_OPTS = {"--root", "--project", "--scope", "--sessions", "--run", "--date", "--id", "--decision", "--edited-file",
               "--remap", "--user-claude-md", "--out", "--pending", "--port", "--idle-minutes", "--max-agents"}
_FLAGS = {"--json", "--dry-run", "--finish", "--no-browser", "-h", "--help"}


def _join_dash_values(argv: list[str]) -> list[str]:
    """`--scope -Users-jo-app` -> `--scope=-Users-jo-app`.

    On macOS and Linux every store name starts with a hyphen (the project path starts with
    `/`), and argparse would read such a value as another option.
    """
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a in _VALUE_OPTS and nxt is not None and nxt.startswith("-") and nxt.split("=")[0] not in _VALUE_OPTS | _FLAGS:
            out.append(f"{a}={nxt}")
            i += 2
        else:
            out.append(a)
            i += 1
    return out


def main(argv=None):
    global ROOT
    for stream in (sys.stdout, sys.stderr):   # report text carries em dashes and arrows; cp1252 consoles choke on them
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="data root (digests/, runs/, reports/, snapshots/, archive/); default: this checkout")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("projects"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_projects)
    s = sub.add_parser("pending"); s.add_argument("--project", default="all"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_pending)
    s = sub.add_parser("digest-prepare"); s.add_argument("--project", default="all")
    s.add_argument("--max-agents", type=int, default=0, help="worker budget for this batch (default: config digest_batch_agents)")
    s.add_argument("--sessions", help="comma-separated session id prefixes; digest only these"); s.set_defaults(fn=cmd_digest_prepare)
    s = sub.add_parser("digest-finish"); s.add_argument("--run", required=True); s.set_defaults(fn=cmd_digest_finish)
    s = sub.add_parser("dream-prepare"); s.add_argument("--scope", default="all"); s.set_defaults(fn=cmd_dream_prepare)
    s = sub.add_parser("extract"); s.add_argument("session"); s.add_argument("--out", required=True); s.set_defaults(fn=cmd_extract)
    s = sub.add_parser("extract-all"); s.add_argument("--pending", required=True); s.add_argument("--run", required=True); s.set_defaults(fn=cmd_extract_all)
    s = sub.add_parser("bundle"); s.add_argument("--scope", required=True); s.add_argument("--run", required=True); s.set_defaults(fn=cmd_bundle)
    s = sub.add_parser("assemble"); s.add_argument("--run", required=True); s.add_argument("--date"); s.set_defaults(fn=cmd_assemble)
    s = sub.add_parser("report"); s.add_argument("report", nargs="?", default="latest"); s.set_defaults(fn=cmd_report)
    s = sub.add_parser("render"); s.add_argument("report"); s.set_defaults(fn=cmd_render)
    s = sub.add_parser("serve"); s.add_argument("report", nargs="?", default="latest"); s.add_argument("--port", type=int, default=0)
    s.add_argument("--idle-minutes", type=int, default=0); s.add_argument("--no-browser", action="store_true"); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("decide"); s.add_argument("report"); s.add_argument("--id", required=True)
    s.add_argument("--decision", required=True, choices=["accept", "reject", "skip"]); s.add_argument("--edited-file")
    s.add_argument("--finish", action="store_true"); s.set_defaults(fn=cmd_decide)
    s = sub.add_parser("apply"); s.add_argument("report", nargs="?", default="latest"); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--remap", action="append", help="STORE=PATH, for testing against a copy"); s.add_argument("--user-claude-md"); s.set_defaults(fn=cmd_apply)

    args = ap.parse_args(_join_dash_values(sys.argv[1:] if argv is None else list(argv)))
    if args.root:
        ROOT = Path(args.root).resolve()
    cfg = load_config()
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
