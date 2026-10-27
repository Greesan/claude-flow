"""The charts page: one HTML page with every saved flow on this machine, rebuilt whenever a flow gets a new version.

Building is local and automatic (`refresh()` runs after every applied edit). Publishing is not: the page is hosted as a
claude.ai artifact, which only Claude can publish. So `refresh()` reports when the page changed and where it is
published, the edit's result passes that on to Claude, and the session-start hook repeats it until
`flow charts --published <url>` records that the current page is live.

~/.claude/flows/.charts.json  {"projects": [dirs with flows], "run_projects": [dirs where a global flow ran],
                               "artifact": url, "published": sha of the live page}
~/.claude/flows/charts.html   the page
"""
import contextlib
import hashlib
import html
import json
import os
import re
import time
from pathlib import Path

from .mermaid import to_mermaid

E = html.escape
TEMPLATE = Path(__file__).with_name("charts_page.html")


def _home():
    return Path.home() / ".claude" / "flows"


def page_path():
    return _home() / "charts.html"


def _config():
    p = _home() / ".charts.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def _save(cfg):
    from .store import _atomic_write
    _home().mkdir(parents=True, exist_ok=True)
    _atomic_write(_home() / ".charts.json", json.dumps(cfg, indent=1))


def register_project(project):
    """Remember a project that has flows, so the page includes them when built from anywhere."""
    project = str(Path(project).resolve())
    cfg = _config()
    if project not in cfg.setdefault("projects", []):
        cfg["projects"].append(project)
        _save(cfg)


def register_run_project(project):
    """Remember a project where a global flow ran: its runs are kept there, and the page pools them."""
    project = str(Path(project).resolve())
    cfg = _config()
    if project not in cfg.setdefault("run_projects", []):
        cfg["run_projects"].append(project)
        _save(cfg)


@contextlib.contextmanager
def _in_project(project):
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old


def _where(d):
    d = Path(d)
    return "global" if d.parent == _home() else d.parents[2].name


def _global_timing(name):
    """(timing, number of projects with runs) for a global flow, from its runs in every project it ran in."""
    from .store import SCOPES, FlowStore, timing_of
    cfg, runs, where = _config(), [], 0
    for project in dict.fromkeys(cfg.get("run_projects", []) + cfg.get("projects", [])):
        if not Path(project).is_dir():
            continue
        with _in_project(project):
            found = FlowStore(name, scope=SCOPES[1]).runs()
        runs += found
        where += bool(found)
    return timing_of(runs), where


def _section(s, label):
    from .store import callers, resolve_flow
    g, where = s.load(), None
    tm = s.timing() if label != "global" else None
    if label == "global":
        tm, where = _global_timing(s.name)
    heads = {}
    for c in g.get("uses") or {}:
        try:
            heads[c] = resolve_flow(c)[0].head_version()
        except Exception:  # noqa: BLE001 - a broken pin shows as "?" rather than breaking the page
            pass
    chart = to_mermaid(g, s.recover_stats(), None, tm, heads)
    links = dict(re.findall(r"%% link (\S+) -> (\S+)", chart))
    tips = dict(re.findall(r"%% tip (\S+) (.*)", chart))
    ins = ", ".join(f"<code>{E(k)}</code>" + (f" = {E(str(v['default']))}" if v.get("default") is not None else " (required)")
                    for k, v in (g.get("inputs") or {}).items()) or "none"
    rows = [("Inputs", ins), ("Returns", ", ".join(f"<code>{E(k)}</code>" for k in (g.get("outputs") or {})) or "nothing")]
    calls = [f'<a href="#flow-{E(c)}"><code>{E(c)}</code></a> pinned v{p["version"]}'
             + (f" (v{heads[c]} available)" if heads.get(c, 0) > p["version"] else "") for c, p in (g.get("uses") or {}).items()]
    if calls:
        rows.append(("Calls", ", ".join(calls)))
    used = callers(s)
    if used:
        rows.append(("Used by", ", ".join(f'<a href="#flow-{E(c["flow"])}"><code>{E(c["flow"])}</code></a> '
                                          f'({E(_where(c["dir"]))}, pins v{c["version"]})' for c in used)))
    kinds = {}
    for k, n in g["nodes"].items():
        group = {"run": n.get("effect"), "decide": "decisions", "flow": "runs another flow"}.get(n.get("type", "run"))
        if group:
            kinds.setdefault(group, []).append(k)
    rows.append(("Steps", " · ".join(f"<b>{E(k)}</b>: {', '.join(E(x) for x in v)}" for k, v in kinds.items())))
    if tm and tm["runs"]:
        rows.append(("Timing here" if where is None else "Timing", f"avg {tm['mean']}s, p90 {tm['p90']}s over {tm['runs']} runs"
                     + (f" in {where} project{'s' * (where != 1)}" if where is not None else "")))
    rec = [(k, r) for k, n in g["nodes"].items() for r in n.get("recover") or []]
    table = ""
    if rec:
        table = ('<div class="scroll"><table><thead><tr><th>Recovery path</th><th>On step</th><th>When</th><th>Picked by</th>'
                 "</tr></thead><tbody>" + "".join(
                     f"<tr><td><code>{E(r['id'])}</code></td><td><code>{E(k)}</code></td><td>{E(r['when'])}</td>"
                     f"<td>{'regex' if r.get('match') else 'decision model'}</td></tr>" for k, r in rec) + "</tbody></table></div>")
    dl = "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in rows)
    tag = "global (every project)" if label == "global" else f"{label} project"
    return f"""<section id="flow-{E(s.name)}">
  <div class="head"><h2>{E(s.name)}</h2><span class="tag">v{g['version']} · {E(tag)}</span></div>
  <p>{E(g.get('about', ''))}</p>
  <dl>{dl}</dl>
  <div class="chart" data-links="{E(json.dumps(links))}" data-tips="{E(json.dumps(tips))}"><pre class="mermaid">{E(chart.rstrip())}
</pre></div>
  {table}
</section>"""


def build():
    """(html, [flow names]) for every flow in the registered projects, then the global ones."""
    from .store import FlowStore, SCOPES, flow_roots
    cfg, sections, names, keep = _config(), [], [], []
    for project in cfg.get("projects", []):
        with _in_project(project):
            root = flow_roots()[0]
            found = sorted(p.parent.name for p in root.glob("*/flow.yaml")) if root.is_dir() else []
            if found:
                keep.append(project)
            for name in found:
                if name not in names:
                    sections.append(_section(FlowStore(name, scope=SCOPES[0]), Path(project).name))
                    names.append(name)
    if keep != cfg.get("projects", []):
        cfg["projects"] = keep
        _save(cfg)
    with _in_project(keep[0] if keep else str(Path.home())):
        root = flow_roots()[1]
        for name in sorted(p.parent.name for p in root.glob("*/flow.yaml")) if root.is_dir() else []:
            if name not in names:
                sections.append(_section(FlowStore(name, scope=SCOPES[1]), "global"))
                names.append(name)
    nav = " · ".join(f'<a href="#flow-{E(n)}"><code>{E(n)}</code></a>' for n in names) or "none yet"
    page = (TEMPLATE.read_text().replace("{{NAV}}", nav)
            .replace("{{SECTIONS}}", "\n".join(sections) or "<p>No saved flows yet.</p>"))
    return page, names


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def status():
    """{path, artifact, stale}: stale = the page on disk differs from what was last published."""
    cfg, p = _config(), page_path()
    page = p.read_text() if p.exists() else ""
    return {"path": str(p), "artifact": cfg.get("artifact"), "stale": bool(page) and _sha(_body(page)) != cfg.get("published")}


def _body(page):
    return re.sub(r"\{\{BUILT\}\}|<!--built [^>]*-->", "", page)


def refresh():
    """Rebuild the page; returns a note for an edit's result when it changed, else None. Never raises."""
    try:
        page, _ = build()
        p = page_path()
        old = p.read_text() if p.exists() else ""
        if _body(old) == _body(page.replace("{{BUILT}}", "")):
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(page.replace("{{BUILT}}", f"<!--built {time.strftime('%F %T')}-->"))
        st = status()
        how = (f"republish {st['path']} to {st['artifact']} (Artifact tool, url={st['artifact']}), then "
               f"`flow charts --published {st['artifact']}`" if st["artifact"] else
               f"publish {st['path']} as an artifact if the user wants the charts page, then `flow charts --published <url>`")
        return {"updated": st["path"], "next": how}
    except Exception as e:  # noqa: BLE001 - a chart problem must never block an edit
        return {"error": f"charts page not rebuilt: {type(e).__name__}: {e}"}


def mark_published(url):
    p = page_path()
    if not p.exists():
        refresh()
    cfg = _config()
    cfg["artifact"], cfg["published"] = url, _sha(_body(p.read_text()))
    _save(cfg)
    return {"status": "ok", "artifact": url, "page": str(p)}
