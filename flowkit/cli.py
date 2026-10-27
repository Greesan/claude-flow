"""flow: state-graph runner for Claude Code.

Running
  flow list
  flow run <flow> [-i key=value ...] [--inputs-json '{...}'] [--detach]
  flow resume <flow> [--run ID] (--approve | --done | --choose <option> | --abort) [--detach]
  flow status <flow> [--run ID] [--wait SECONDS]          flow runs <flow>
  flow learn <flow> [--run ID] --propose          the failure + the trace of what Claude did since the pause
  flow learn <flow> [--run ID] --patch-json '{...}' | --patch f.json
                                          verify a patch (ops + rationale + trace evidence), then apply or queue it
  flow learn <flow> [--run ID] --replay --at <node> --when "..." [--from-trace] [--cmd "<c>" ...] [--match RE]

Finding flows to automate (local, no model calls)
  flow mine [--file transcript.jsonl ...]  parse new/changed session transcripts, rebuild candidates
  flow candidates [--all] [--json]         table of candidates and why; --all adds patterns
  flow candidate show|dismiss|done <id>
  flow usage [<flow>]                      how often each flow ran (main agent vs subagents) and how runs ended
  flow draft <id> --as <flow>              evidence + skeleton for Claude to write the flow
  flow hook session-start | pre-compact | session-end | approve-guard   (Claude Code hooks)   flow hooks (print config)

Reading
  flow show <flow> [--version N]         flow validate <flow>
  flow export <flow> [--version N] [--run ID] [--out f.mmd]
  flow charts [--published URL]          rebuild the page of every flow's chart (automatic after each edit);
                                         --published records that the current page is live at URL
  flow history <flow>                    flow diff <flow> <vA> <vB>

Editing (every edit is validated, versioned and logged; --base N = the version you looked at)
  flow new <flow> [--about TEXT] [--global]
  flow import <flow> <file.yaml> [--global]
  flow commit <flow>                      (record hand edits made to flow.yaml)
  flow revert <flow> --to N
  flow upgrade <flow> [<called flow>] [--to N]   move the pinned version of flows it calls (checked first)
  flow promote <flow>                     move a project flow to ~/.claude/flows so other projects can call it
  flow node add <flow> <id> --json '{...}'      flow node set <flow> <id> --json '{"key": value|null}'
  flow node rm <flow> <id> [--rewire NODE]      flow node rename <flow> <id> <new_id>
  flow edge <flow> <node> ok|next|fail|branch:<opt> <target|human: hint|none>
  flow recover add <flow> <node> --json '{...}' flow recover rm|promote|drop <flow> <node> <rid>
  flow input add <flow> <name> --json '{...}'   flow input rm <flow> <name>
  flow meta <flow> --json '{"about": ..., "env": {...}, "secrets_from": {...}}'
  flow proposals <flow>                   flow accept|reject <flow> <proposal_id>
  Edit options: -m/--reason TEXT, --author NAME (default $FLOW_AUTHOR or "claude"), --base N
"""
import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import charts, engine, hooks, learn, mine
from .mermaid import to_mermaid
from .model import FlowError, dump_yaml, load_yaml, resolve_inputs, validate
from .store import FlowStore, callers, list_flows, resolve_flow, resolver

EXIT = 0
BAD = {"error", "rejected", "failed"}


def out(d):
    global EXIT
    if isinstance(d, dict) and d.get("status") in BAD:
        EXIT = 1
    print(json.dumps(d, default=str))


def signature(g):
    """What a flow takes and gives back, as tables (comment lines, so the YAML below still parses)."""
    rows = ["# Inputs", "# | name | type | default | meaning |", "# |---|---|---|---|"]
    for k, v in (g.get("inputs") or {}).items():
        t = v.get("type", "string") + (f" ({' / '.join(map(str, v['values']))})" if v.get("values") else "")
        d = v.get("default", "required" if v.get("required", True) else "")
        rows.append(f"# | {k} | {t} | {d} | {v.get('help', '')} |")
    rows += ["#", "# Outputs (in the result's `outputs` when the run ends)", "# | name | from | meaning |", "# |---|---|---|"]
    rows += [f"# | {k} | {v.get('from')} | {v.get('help', '')} |" for k, v in (g.get("outputs") or {}).items()]
    ends = [k for k, n in g["nodes"].items() if n.get("type") == "end"]
    rows += ["#", f"# Ends: {', '.join(ends)} (the result's `ended` says which)"]
    return "\n".join(rows) + "\n"


def _child_heads(g):
    heads = {}
    for name in g.get("uses") or {}:
        try:
            heads[name] = resolve_flow(name)[0].head_version()
        except FlowError:
            pass
    return heads


def _calls(s, g):
    """'Calls' and 'Used by' lines for `flow show` (comment lines, so the YAML below still parses)."""
    heads, rows = _child_heads(g), []
    for name, pin in (g.get("uses") or {}).items():
        behind = f", v{heads[name]} available: `flow upgrade {s.name} {name}`" if heads.get(name, 0) > pin["version"] else ""
        rows.append(f"#   {name} ({pin['scope']}) pinned v{pin['version']}{behind}")
    out = ["#", "# Calls:", *rows] if rows else []
    used = callers(s)
    if used:
        def where(d):
            return "global" if Path(d).parent == Path.home() / ".claude" / "flows" else Path(d).parents[2].name
        out += ["#", "# Used by:"] + [f"#   {where(c['dir'])}/{c['flow']} pins v{c['version']}"
                                     + (f" (head is v{s.head_version()})" if c["version"] < s.head_version() else "")
                                     for c in used]
    return "\n".join(out) + ("\n" if out else "")


def _store(name):
    s = FlowStore(name)
    if not s.exists():
        raise FlowError(f"no flow named '{name}'")
    return s


def _edit(store, ops, a):
    out(store.apply(ops, a.author, a.reason or " ".join(sys.argv[1:4]), base=a.base))


def _parse_kv(pairs):
    d = {}
    for p in pairs or []:
        if "=" not in p:
            raise FlowError(f"-i expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        d[k] = v
    return d


def detach(s, st, flags=()):
    """Run (or resume) in a background process; the result lands in the run file for `flow status --wait`."""
    st["status"] = "running"
    st.pop("result", None)
    s.save_run(st)
    log = open(s.runs_dir / f"{st['run']}.log", "w")
    subprocess.Popen([sys.executable, "-m", "flowkit", "_work", s.name, st["run"], *flags], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return {"status": "running", "run": st["run"], "detached": True,
            "wait": f"flow status {s.name} --run {st['run']} --wait 540"}


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "hook":
        global EXIT
        EXIT = hooks.run(rest[0] if rest else "")
        return
    if cmd == "hooks":
        return print(json.dumps(hooks.config(), indent=2))
    if cmd == "_work":  # the background half of --detach
        s = _store(rest[0])
        st = s.load_run(rest[1])
        return engine.resume(s, st, rest[2:]) if rest[2:] else engine.run(s, st)
    if cmd == "mine":
        ap = argparse.ArgumentParser(prog="flow mine")
        ap.add_argument("--file", action="append")
        return out(mine.mine(ap.parse_args(rest).file))
    if cmd == "usage":
        return out(mine.usage(rest[0] if rest else None))
    if cmd == "candidates":
        rows = mine.list_candidates("--all" in rest)
        return out({"candidates": rows}) if "--json" in rest else sys.stdout.write(mine.table(rows))
    if cmd == "candidate":
        if len(rest) < 2 or rest[0] not in ("show", "dismiss", "done"):
            raise FlowError("flow candidate show|dismiss|done <id>")
        c = mine.get_candidate(rest[1])
        if not c:
            raise FlowError(f"no candidate {rest[1]} (run `flow mine`)")
        if rest[0] == "show":
            return out(c)
        mine.set_status(rest[1], "dismissed" if rest[0] == "dismiss" else "done")
        return out({"status": rest[0] if rest[0] == "done" else "dismissed", "candidate": rest[1]})
    if cmd == "draft":
        ap = argparse.ArgumentParser(prog="flow draft")
        ap.add_argument("id")
        ap.add_argument("--as", dest="name", required=True)
        a = ap.parse_args(rest)
        c = mine.get_candidate(a.id)
        if not c:
            raise FlowError(f"no candidate {a.id} (run `flow mine`)")
        mine.set_status(a.id, "drafting", a.name)
        return out(mine.draft_brief(c, a.name))
    if cmd == "charts":
        ap = argparse.ArgumentParser(prog="flow charts")
        ap.add_argument("--published", metavar="URL", help="record that the current page is live at URL")
        a = ap.parse_args(rest)
        if any(f.scope() == "project" for f in list_flows()):
            charts.register_project(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        if a.published:
            return out(charts.mark_published(a.published))
        return out({"status": "ok", "changed": charts.refresh(), **charts.status()})
    if cmd == "list":
        return out({"flows": [{"name": s.name, "version": s.head_version(), "about": s.load().get("about", ""),
                               "inputs": list((s.load().get("inputs") or {})),
                               "outputs": list((s.load().get("outputs") or {})),
                               "calls": {k: v["version"] for k, v in (s.load().get("uses") or {}).items()},
                               "used_by": [f"{c['flow']}@v{c['version']}" for c in callers(s)],
                               "timing": s.timing(), "where": str(s.dir)}
                              for s in list_flows()]})

    ap = argparse.ArgumentParser(prog=f"flow {cmd}")
    edit = argparse.ArgumentParser(add_help=False)
    edit.add_argument("-m", "--reason", default="")
    edit.add_argument("--author", default=os.environ.get("FLOW_AUTHOR", "claude"))
    edit.add_argument("--base", type=int)

    if cmd == "run":
        ap.add_argument("flow")
        ap.add_argument("-i", "--input", action="append")
        ap.add_argument("--inputs-json")
        ap.add_argument("--detach", action="store_true", help="run in a background process (outside Claude Code)")
        a = ap.parse_args(rest)
        s = _store(a.flow)
        flow = s.load()
        given = {**(json.loads(a.inputs_json) if a.inputs_json else {}), **_parse_kv(a.input)}
        inputs = resolve_inputs(flow, given)
        st = s.new_run(flow["version"], inputs, flow["start"])
        if a.detach:
            return out(detach(s, st))
        res = engine.run(s, st)
        if s.uncommitted():
            res["warning"] = f"flow.yaml has uncommitted hand edits; this run used v{flow['version']}"
        return out(res)

    if cmd == "resume":
        ap.add_argument("flow")
        ap.add_argument("--run")
        ap.add_argument("--detach", action="store_true")
        a, flags = ap.parse_known_args(rest)
        s = _store(a.flow)
        st = s.load_run(a.run)
        if not st or st.get("status") != "paused":
            return out({"status": "error", "error": "nothing paused to resume", "runs": [
                {"run": r["run"], "status": r["status"]} for r in s.runs()[-5:]]})
        return out(detach(s, st, flags) if a.detach else engine.resume(s, st, flags))

    if cmd in ("status", "runs"):
        ap.add_argument("flow")
        ap.add_argument("--run")
        ap.add_argument("--wait", type=int, default=0, help="seconds to wait for a running (detached) run")
        a = ap.parse_args(rest)
        s = _store(a.flow)
        if cmd == "runs":
            return out({"runs": [{k: r.get(k) for k in ("run", "status", "wait", "version", "current", "inputs")}
                                 for r in s.runs()]})
        st = s.load_run(a.run) if a.run else (s.runs()[-1] if s.runs() else None)
        deadline = time.time() + min(a.wait, 570)
        while st and st.get("status") == "running" and time.time() < deadline:
            time.sleep(1)
            st = s.load_run(st["run"])
        if not st:
            return out({"status": "idle"})
        if st.get("status") == "running":
            return out({"status": "running", "run": st["run"], "at": st.get("current"),
                        "wait": f"flow status {s.name} --run {st['run']} --wait 540"})
        return out(st.get("result") or st)

    if cmd == "learn":
        ap.add_argument("flow")
        ap.add_argument("--run")
        ap.add_argument("--propose", action="store_true")
        ap.add_argument("--patch")
        ap.add_argument("--patch-json")
        ap.add_argument("--replay", action="store_true")
        ap.add_argument("--at")
        ap.add_argument("--when")
        ap.add_argument("--from-trace", action="store_true")
        ap.add_argument("--cmd", action="append")
        ap.add_argument("--match")
        a = ap.parse_args(rest)
        s = _store(a.flow)
        st = s.load_run(a.run)
        if not st:
            return out({"status": "error", "error": "learn needs a paused run (it learns what fixed that run)"})
        if a.propose:
            return out(learn.learn_propose(s, st))
        if a.patch or a.patch_json:
            patch = json.loads(Path(a.patch).read_text() if a.patch else a.patch_json)
            return out(learn.learn_patch(s, st, patch))
        if a.replay:
            if not (a.at and a.when):
                raise FlowError("--replay needs --at <node> and --when \"<situation>\"")
            return out(learn.learn_replay(s, st, a.at, a.when, a.cmd, a.from_trace, a.match))
        raise FlowError("flow learn needs --propose, --patch/--patch-json, or --replay")

    if cmd in ("show", "validate", "export", "history", "proposals"):
        ap.add_argument("flow")
        ap.add_argument("--version", type=int)
        ap.add_argument("--run")
        ap.add_argument("--out")
        a = ap.parse_args(rest)
        s = _store(a.flow)
        g = s.load(a.version)
        if cmd == "show":
            sys.stdout.write(signature(g) + _calls(s, g) + "\n" + dump_yaml(g))
            tm = s.timing()
            if tm:
                sys.stdout.write(f"# timing here: run avg {tm['mean']}s, p90 {tm['p90']}s over {tm['runs']} runs; per step: "
                                 + ", ".join(f"{k} avg {v['mean']}s" for k, v in tm["steps"].items()) + "\n")
            stats = s.recover_stats()
            if stats:
                sys.stdout.write("# evidence (recovery path: ok / failed): "
                                 + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in stats.items()) + "\n")
            return
        if cmd == "validate":
            errs = validate(g, s.dir, resolver(g.get("uses")))
            return out({"status": "ok" if not errs else "invalid", "version": g["version"], "errors": errs,
                        "uncommitted_hand_edits": s.uncommitted()})
        if cmd == "history":
            return out({"history": [{k: h[k] for k in ("version", "base", "rebased", "author", "reason", "notes")}
                                    | {"ops": [o["op"] for o in h["ops"]]} for h in s.history()]})
        if cmd == "proposals":
            return out({"proposals": [{k: p[k] for k in ("id", "base", "author", "reason", "why")}
                                      | {"ops": [o["op"] for o in p["ops"]]} for p in s.proposals()]})
        st = s.load_run(a.run) if a.run else None
        text = to_mermaid(g, s.recover_stats(), (st or {}).get("current"), s.timing(), _child_heads(g))
        if a.out:
            Path(a.out).write_text(text)
            return out({"status": "ok", "wrote": a.out, "version": g["version"]})
        sys.stdout.write(text)
        return

    if cmd == "diff":
        ap.add_argument("flow")
        ap.add_argument("a", type=int)
        ap.add_argument("b", type=int)
        a = ap.parse_args(rest)
        s = _store(a.flow)
        sys.stdout.writelines(difflib.unified_diff(dump_yaml(s.load(a.a)).splitlines(True),
                                                   dump_yaml(s.load(a.b)).splitlines(True), f"v{a.a}", f"v{a.b}"))
        return

    # ----- editing -----
    ap = argparse.ArgumentParser(prog=f"flow {cmd}", parents=[edit])
    if cmd in ("new", "import"):
        ap.add_argument("flow")
        if cmd == "import":
            ap.add_argument("file")
        ap.add_argument("--about", default="")
        ap.add_argument("--global", dest="glob", action="store_true")
        a = ap.parse_args(rest)
        s = FlowStore(a.flow, create_global=a.glob)
        if cmd == "new":
            if s.exists():
                return out({"status": "error", "error": f"{a.flow} already exists"})
            g = {"name": a.flow, "about": a.about, "inputs": {}, "start": "done",
                 "nodes": {"done": {"type": "end", "say": "Done."}}}
        else:
            src = Path(a.file).resolve()
            g = load_yaml(src.read_text())
            if (src.parent / "steps").is_dir() and src.parent != s.dir.resolve():  # bring the flow's scripts along
                shutil.copytree(src.parent / "steps", s.dir / "steps", dirs_exist_ok=True)
        return out(s.apply([{"op": "replace_graph", "graph": g}], a.author, a.reason or f"{cmd} {a.flow}"))

    if cmd == "commit":
        ap.add_argument("flow")
        a = ap.parse_args(rest)
        return out(_store(a.flow).commit_hand_edit(a.author, a.reason or "hand edit"))

    if cmd == "upgrade":
        ap.add_argument("flow")
        ap.add_argument("child", nargs="?")
        ap.add_argument("--to", type=int)
        a = ap.parse_args(rest)
        s = _store(a.flow)
        uses = s.load().get("uses") or {}
        names = [a.child] if a.child else list(uses)
        ops = []
        for name in names:
            if name not in uses:
                raise FlowError(f"{a.flow} does not call {name}")
            want = a.to or FlowStore(name, scope=uses[name]["scope"]).head_version()
            if want != uses[name]["version"]:
                ops.append({"op": "pin_flow", "flow": name, "version": want})
        if not ops:
            return out({"status": "unchanged", "uses": uses})
        return _edit(s, ops, a)

    if cmd == "promote":
        ap.add_argument("flow")
        a = ap.parse_args(rest)
        return out(_store(a.flow).promote(a.author))

    if cmd == "revert":
        ap.add_argument("flow")
        ap.add_argument("--to", type=int, required=True)
        a = ap.parse_args(rest)
        s = _store(a.flow)
        return _edit(s, [{"op": "replace_graph", "graph": s.load(a.to)}], a)

    if cmd in ("accept", "reject"):
        ap.add_argument("flow")
        ap.add_argument("proposal")
        a = ap.parse_args(rest)
        return out(_store(a.flow).resolve_proposal(a.proposal, cmd == "accept", a.author))

    if cmd == "edge":
        ap.add_argument("flow")
        ap.add_argument("node")
        ap.add_argument("kind")
        ap.add_argument("to")
        a = ap.parse_args(rest)
        return _edit(_store(a.flow), [{"op": "set_edge", "id": a.node, "kind": a.kind,
                                       "to": None if a.to == "none" else a.to}], a)

    if cmd == "meta":
        ap.add_argument("flow")
        ap.add_argument("--json", required=True)
        a = ap.parse_args(rest)
        return _edit(_store(a.flow), [{"op": "set_meta", **json.loads(a.json)}], a)

    if cmd in ("node", "recover", "input"):
        if not rest:
            raise FlowError(f"flow {cmd} needs a sub-command")
        sub, rest = rest[0], rest[1:]
        ap.prog = f"flow {cmd} {sub}"
        ap.add_argument("flow")
        if cmd == "node":
            ap.add_argument("id")
            if sub == "rename":
                ap.add_argument("to")
            ap.add_argument("--json")
            ap.add_argument("--rewire")
            a = ap.parse_args(rest)
            op = {"add": lambda: {"op": "add_node", "id": a.id, "node": json.loads(a.json)},
                  "set": lambda: {"op": "update_node", "id": a.id, "set": json.loads(a.json)},
                  "rm": lambda: {"op": "remove_node", "id": a.id, "rewire": a.rewire},
                  "rename": lambda: {"op": "rename_node", "id": a.id, "to": a.to}}
        elif cmd == "recover":
            ap.add_argument("node")
            if sub != "add":
                ap.add_argument("rid")
            ap.add_argument("--json")
            a = ap.parse_args(rest)
            op = {"add": lambda: {"op": "add_recover", "node": a.node, "entry": json.loads(a.json)},
                  "rm": lambda: {"op": "remove_recover", "node": a.node, "rid": a.rid},
                  "promote": lambda: {"op": "promote_recover", "node": a.node, "rid": a.rid},
                  "drop": lambda: {"op": "drop_recover", "node": a.node, "rid": a.rid}}
        else:
            ap.add_argument("name")
            ap.add_argument("--json")
            a = ap.parse_args(rest)
            op = {"add": lambda: {"op": "add_input", "name": a.name, "spec": json.loads(a.json)},
                  "rm": lambda: {"op": "remove_input", "name": a.name}}
        if sub not in op:
            raise FlowError(f"flow {cmd} sub-commands: {sorted(op)}")
        return _edit(_store(a.flow), [op[sub]()], a)

    out({"status": "error", "error": f"unknown command {cmd}; see `flow help`"})


def entry():
    try:
        main(sys.argv[1:])
    except FlowError as e:
        out({"status": "error", "error": str(e)})
    sys.exit(EXIT)
