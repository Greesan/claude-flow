"""Flow storage: immutable versions, a per-flow lock, history, proposals, events, and per-run state.

Layout of one flow (in <project>/.claude/flows/<name>/, or ~/.claude/flows/<name>/ for shared flows):
  flow.yaml            the head version (what you read and hand-edit; `flow commit` records hand edits)
  versions/vNNNN.yaml  every version ever applied, immutable
  steps/*.sh           scripts referenced by `script:` steps
  history.jsonl        one line per applied batch of ops: version, base, author, reason, ops
  proposals/<id>.json  op batches that could not be applied automatically (conflicts), awaiting review
  events.jsonl         append-only facts: recovery outcomes, decision labels (evidence, never conflicts)
  decisions.jsonl      every decision-model (Laya/Jev) call
Run state lives with the project that ran it: <project>/.claude/flows/.runs/<name>/<run_id>.json.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import secrets as _rand
import shutil
import time
from pathlib import Path

from . import charts
from .model import FlowError, called, dump_yaml, load_yaml, validate
from .ops import ADDITIVE, Merged, OpError, apply_op, touched


def project_dir():
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def flow_roots():
    return [project_dir() / ".claude" / "flows", Path.home() / ".claude" / "flows"]


SCOPES = ("project", "global")
def _callers_file():
    return Path.home() / ".claude" / "flows" / ".callers.json"


def resolve_flow(name):
    """(FlowStore, scope) for a flow another flow calls. A name defined both in this project and globally is an error:
    a called flow must be unambiguous, so a project flow can never quietly stand in for the shared one."""
    found = [sc for sc, r in zip(SCOPES, flow_roots()) if (r / name / "flow.yaml").exists()]
    if not found:
        raise FlowError(f"no flow named '{name}' in this project or in ~/.claude/flows")
    if len(found) > 1:
        raise FlowError(f"'{name}' exists both in this project and in ~/.claude/flows; rename the project one, or "
                        f"delete it if the global one replaces it (a called flow must be unambiguous)")
    return FlowStore(name, scope=found[0]), found[0]


def resolver(pins=None):
    """resolve(name, version) for validate(): loads a called flow at its pinned version (head when unpinned)."""
    def resolve(name, version=None):
        st, scope = resolve_flow(name)
        want = (pins or {}).get(name, {}).get("scope")
        if want and want != scope:
            raise FlowError(f"'{name}' is pinned to the {want} flow, but only a {scope} one exists now "
                            f"(was it moved? `flow upgrade` re-pins it)")
        return st.load(version), scope
    return resolve


def callers(store):
    """Who calls this flow, from the registry: [{dir, flow, version}] (one per calling flow, in any project)."""
    data = json.loads(_callers_file().read_text()) if _callers_file().exists() else {}
    return sorted(data.get(str(store.dir), {}).values(), key=lambda c: (c["dir"], c["flow"]))


def _register_calls(store, g):
    """Record this flow's pins in the callers registry (and forget pins it no longer has)."""
    _callers_file().parent.mkdir(parents=True, exist_ok=True)
    with open(_callers_file().parent / ".callers.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        data = json.loads(_callers_file().read_text()) if _callers_file().exists() else {}
        me = str(store.dir)
        for child in data.values():
            child.pop(me, None)
        for name, pin in (g.get("uses") or {}).items():
            child_dir = str(FlowStore(name, scope=pin["scope"]).dir)
            data.setdefault(child_dir, {})[me] = {"dir": me, "flow": store.name, "version": pin["version"]}
        _atomic_write(_callers_file(), json.dumps({k: v for k, v in data.items() if v}, indent=1))


def list_flows():
    seen, out = set(), []
    for root in flow_roots():
        for f in sorted(root.glob("*/flow.yaml")) if root.is_dir() else []:
            if f.parent.name not in seen:
                seen.add(f.parent.name)
                out.append(FlowStore(f.parent.name))
    return out


def _atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


GITIGNORE = """# flow runtime state (per machine); flow.yaml, versions/, steps/ and history.jsonl are the definition
.runs/
*/.lock
*/events.jsonl
*/decisions.jsonl
*/proposals/
"""


def timing_of(runs):
    """Measured durations (None if nothing measured yet): per run and per step, the average, median and p90 in
    seconds. Run totals come from finished runs only, each the sum of its own step times, so runs that took
    different paths (recoveries, skipped checks) are counted as they actually happened. Step times come from every
    run, failed and paused ones too: a step that ran is measured whatever happened after it. Waits for a person
    (confirm, human, Claude's choices) are never timed. Nothing extra is recorded."""
    totals, steps = [], {}
    for r in runs:
        ds = [e for e in r.get("log", []) if isinstance(e.get("s"), (int, float))]
        if r.get("status") == "done":
            totals.append(sum(e["s"] for e in ds))
        for e in ds:
            steps.setdefault(e["node"], []).append(e["s"])
    if not steps:
        return None

    def pct(xs, q):
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1)

    def summary(xs):
        return {"runs": len(xs), "mean": round(sum(xs) / len(xs), 1), "p50": pct(xs, 0.5), "p90": pct(xs, 0.9)}
    return {**(summary(totals) if totals else {"runs": 0}), "steps": {k: summary(v) for k, v in steps.items()}}


def script_hashes(g, flow_dir):
    """{script: sha256} for every `script:` step, so a pinned run can tell when a script changed under it."""
    out = {}
    for n in g.get("nodes", {}).values():
        if n.get("script"):
            p = Path(flow_dir) / "steps" / n["script"]
            out[n["script"]] = hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else None
    return out


def _append(path, obj):
    # one write() of one line with O_APPEND: concurrent appenders cannot interleave inside a line
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (json.dumps(obj) + "\n").encode())
    finally:
        os.close(fd)


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class FlowStore:
    def __init__(self, name, create_global=False, scope=None):
        self.name = name
        roots = flow_roots()
        found = [r / name for r in roots if (r / name / "flow.yaml").exists()]
        if scope:
            self.dir = roots[SCOPES.index(scope)] / name
        else:
            self.dir = found[0] if found else (roots[1] if create_global else roots[0]) / name
        self.runs_dir = project_dir() / ".claude" / "flows" / ".runs" / name

    # ----- definition -----
    def exists(self):
        return self.head_version() > 0

    def head_version(self):
        """versions/ is the source of truth; flow.yaml is a readable (and hand-editable) copy of the head."""
        vs = [int(p.stem[1:]) for p in (self.dir / "versions").glob("v*.yaml")] if (self.dir / "versions").is_dir() else []
        return max(vs, default=0)

    def load(self, version=None):
        v = self.head_version() if version is None else int(version)
        p = self.dir / "versions" / f"v{v:04d}.yaml"
        if not p.exists():
            raise FlowError(f"no flow named '{self.name}' (looked in {[str(r) for r in flow_roots()]})" if v == 0
                            else f"{self.name} has no version {v}")
        return load_yaml(p.read_text())

    def uncommitted(self):
        """flow.yaml or a step script differs from the head version: someone edited it by hand."""
        if not self.exists():
            return False
        f = self.dir / "flow.yaml"
        head = (self.dir / "versions" / f"v{self.head_version():04d}.yaml").read_text()
        if not f.exists() or f.read_text() != head:
            return True
        g = load_yaml(head)
        return (g.get("script_hashes") or {}) != script_hashes(g, self.dir)

    @contextlib.contextmanager
    def lock(self):
        if str(self.dir.resolve()).startswith("/mnt/"):
            raise FlowError(f"{self.dir} is on a Windows drive (/mnt/...), where file locks are unreliable; "
                            "keep flows on the Linux filesystem")
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # held only while a batch of ops is applied: milliseconds
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def history(self):
        return _read_jsonl(self.dir / "history.jsonl")

    def apply(self, ops, author, reason, base=None, _committing=False, meta=None):
        """Apply a batch of ops atomically. Returns a result dict; never leaves a half-applied graph.

        base=None: apply to head (direct edit). base=N (the version the caller saw): if head moved on,
        ADDITIVE ops rebase onto head; other ops whose targets changed since N become a proposal.
        """
        with self.lock():
            if self.uncommitted() and not _committing:
                return {"status": "rejected", "error": "flow.yaml has uncommitted hand edits; run `flow commit "
                        f"{self.name} -m <reason>` (or revert the file) first"}
            fresh = not self.exists()
            head = None if fresh else self.load()
            head_v = 0 if fresh else int(head.get("version", 0))
            stale = base is not None and int(base) != head_v
            if stale:
                changed = set().union(*[touched(op) for h in self.history() if h["version"] > int(base)
                                        for op in h["ops"]] or [set()])
                clash = [op for op in ops if op["op"] not in ADDITIVE and
                         (touched(op) & changed or ("graph",) in changed or op["op"] == "replace_graph")]
                if clash:
                    return self._propose(ops, author, reason, base, meta=meta, why=
                                         f"head moved from v{base} to v{head_v} and changed what these ops edit: "
                                         f"{[op['op'] for op in clash]}")
            g, notes = head, []
            try:
                for op in ops:
                    if g is None and op["op"] != "replace_graph":
                        raise OpError("a new flow starts with replace_graph")
                    try:
                        g, note = apply_op(g if g is not None else {"nodes": {}}, op)
                        if note:
                            notes.append(note)
                    except Merged as m:
                        notes.append(f"merged: {m}")
                if g is head:
                    return {"status": "unchanged", "version": head_v, "notes": notes}
                g["name"] = self.name
                g["version"] = head_v + 1
                g["script_hashes"] = script_hashes(g, self.dir)
                if not g["script_hashes"]:
                    del g["script_hashes"]
                self._pin_calls(g)
                errs = validate(g, self.dir, resolver(g.get("uses")))
                if errs:
                    raise OpError("result would be invalid: " + "; ".join(errs))
            except (OpError, KeyError, TypeError) as e:
                if stale:
                    return self._propose(ops, author, reason, base, meta=meta, why=f"could not rebase onto v{head_v}: {e}")
                return {"status": "rejected", "error": str(e)}
            self._write_version(g)
            if g.get("uses") or (head or {}).get("uses"):
                _register_calls(self, g)
            _append(self.dir / "history.jsonl", {"version": g["version"], "base": base if base is not None else head_v,
                                                "rebased": stale, "author": author, "reason": reason,
                                                "ts": time.time(), "ops": ops, "notes": notes, **({"meta": meta} if meta else {})})
            older = [f"{c['flow']} (pins v{c['version']})" for c in callers(self) if c["version"] < g["version"]]
            if self.scope() == "project":
                charts.register_project(project_dir())
            page = charts.refresh()
            return {"status": "applied", "version": g["version"], "rebased": stale, "notes": notes,
                    **({"callers_on_older_versions": older, "upgrade": "check them, then `flow upgrade <caller> "
                        f"{self.name}`"} if older else {}), **({"charts": page} if page else {})}

    def _pin_calls(self, g):
        """Pin newly called flows at their head version; keep existing pins; drop pins no longer called."""
        uses = {k: v for k, v in (g.get("uses") or {}).items() if k in called(g)}
        for name in called(g):
            if name not in uses:
                try:
                    st, scope = resolve_flow(name)
                except FlowError:
                    continue  # validate() reports it
                uses[name] = {"scope": scope, "version": st.head_version()}
        if uses:
            g["uses"] = uses
        else:
            g.pop("uses", None)

    def scope(self):
        return "global" if self.dir.parent == flow_roots()[1] else "project"

    def promote(self, author="claude"):
        """Move a project flow to ~/.claude/flows so other projects can call it, keeping versions and history.
        Callers in this project are re-pinned to the global copy."""
        if self.scope() == "global":
            raise FlowError(f"{self.name} is already global")
        dest = flow_roots()[1] / self.name
        if (dest / "flow.yaml").exists():
            raise FlowError(f"a global flow named {self.name} already exists; rename one of them first")
        before = callers(self)
        with self.lock():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.dir), str(dest))
        (dest / ".lock").unlink(missing_ok=True)
        old, self.dir = self.dir, dest
        moved = []
        for c in before:
            if Path(c["dir"]).parent != old.parent:
                continue  # other projects could not call a project flow in the first place
            parent = FlowStore(c["flow"], scope="project")
            g = parent.load()
            g["uses"][self.name] = {"scope": "global", "version": g["uses"][self.name]["version"]}
            res = parent.apply([{"op": "replace_graph", "graph": g}], author, f"{self.name} moved to ~/.claude/flows")
            moved.append({"flow": c["flow"], **res})
        data = json.loads(_callers_file().read_text()) if _callers_file().exists() else {}
        data.pop(str(old), None)
        if _callers_file().exists():
            _atomic_write(_callers_file(), json.dumps(data, indent=1))
        return {"status": "promoted", "flow": self.name, "now": str(dest), "callers_repinned": moved}

    def _write_version(self, g):
        vdir = self.dir / "versions"
        vdir.mkdir(parents=True, exist_ok=True)
        text = dump_yaml(g)
        with open(vdir / f"v{g['version']:04d}.yaml", "x") as f:  # 'x': two writers can never share a version
            f.write(text)
        _atomic_write(self.dir / "flow.yaml", text)
        gi = self.dir.parent / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE)

    def commit_hand_edit(self, author, reason):
        """Record a hand-edited flow.yaml as the next version (validated like any other edit)."""
        if not self.uncommitted():
            return {"status": "unchanged", "version": self.head_version(), "notes": ["flow.yaml and steps/ match the head"]}
        g = load_yaml((self.dir / "flow.yaml").read_text())
        # on rejection flow.yaml keeps the hand edit, so it can be fixed and committed again
        return self.apply([{"op": "replace_graph", "graph": g}], author, reason, _committing=True)

    # ----- proposals -----
    def _propose(self, ops, author, reason, base, why, meta=None):
        pdir = self.dir / "proposals"
        pdir.mkdir(parents=True, exist_ok=True)
        pid = time.strftime("%Y%m%d-%H%M%S") + "-" + _rand.token_hex(2)
        (pdir / f"{pid}.json").write_text(json.dumps({"id": pid, "base": base, "author": author, "reason": reason,
                                                      "why": why, "ops": ops, "ts": time.time(),
                                                      **({"meta": meta} if meta else {})}, indent=2))
        return {"status": "proposed", "proposal": pid, "why": why,
                "next": f"flow proposals {self.name}  then  flow accept|reject {self.name} {pid}"}

    def proposals(self):
        pdir = self.dir / "proposals"
        return [json.loads(p.read_text()) for p in sorted(pdir.glob("*.json"))] if pdir.is_dir() else []

    def resolve_proposal(self, pid, accept, author):
        p = self.dir / "proposals" / f"{pid}.json"
        if not p.exists():
            return {"status": "error", "error": f"no proposal {pid}"}
        prop = json.loads(p.read_text())
        if accept:  # re-apply to the current head as a direct edit: it must be valid there
            res = self.apply(prop["ops"], f"{author} (accepted {pid} from {prop['author']})", prop["reason"],
                             meta=prop.get("meta"))
            if res["status"] != "applied" and res["status"] != "unchanged":
                return res
        p.rename(p.with_suffix(".accepted" if accept else ".rejected"))
        return {"status": "accepted" if accept else "rejected", "proposal": pid}

    # ----- evidence -----
    def event(self, **e):
        self.dir.mkdir(parents=True, exist_ok=True)
        _append(self.dir / "events.jsonl", {"ts": time.time(), **e})

    def events(self):
        return _read_jsonl(self.dir / "events.jsonl")

    def recover_stats(self):
        s = {}
        for e in self.events():
            if e.get("type") == "recover_outcome":
                ok, bad = s.get(e["rid"], (0, 0))
                s[e["rid"]] = (ok + 1, bad) if e["ok"] else (ok, bad + 1)
        return s

    def decision(self, rec):
        self.dir.mkdir(parents=True, exist_ok=True)
        _append(self.dir / "decisions.jsonl", rec)

    # ----- runs -----
    def new_run(self, version, inputs, start):
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + _rand.token_hex(3)
        st = {"run": rid, "flow": self.name, "version": version, "inputs": inputs, "current": start,
              "status": "running", "started": time.time(), "log": [], "outputs": {}, "tried": {}, "probation": {}}
        self.save_run(st)
        if self.scope() == "global":
            charts.register_run_project(project_dir())
        return st

    def save_run(self, st):
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        gi = self.runs_dir.parent.parent / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE)
        _atomic_write(self.runs_dir / f"{st['run']}.json", json.dumps(st))

    def load_run(self, run_id=None):
        if run_id:
            p = self.runs_dir / f"{run_id}.json"
            return json.loads(p.read_text()) if p.exists() else None
        paused = [r for r in self.runs() if r["status"] == "paused"]
        if len(paused) > 1:
            raise FlowError(f"{len(paused)} paused runs of {self.name}: pass --run <id> "
                            f"({', '.join(r['run'] for r in paused)})")
        return paused[0] if paused else None

    def trace_path(self, run_id):
        return self.runs_dir / f"{run_id}.trace.jsonl"

    def trace(self, run_id):
        return _read_jsonl(self.trace_path(run_id))

    def runs(self):
        if not self.runs_dir.is_dir():
            return []
        return sorted((json.loads(p.read_text()) for p in self.runs_dir.glob("*.json")), key=lambda r: r["started"])

    def timing(self):
        """Measured durations from finished runs here (None if none yet); see `timing_of`."""
        return timing_of(self.runs())

    def propose(self, ops, author, reason, base, why, meta=None):
        with self.lock():
            return self._propose(ops, author, reason, base, why, meta)
