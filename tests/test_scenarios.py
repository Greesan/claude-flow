"""The shipped git-push-recovery flow against real local git repos (sandboxes/git_repo.sh): no network, no model."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flowkit import engine, secrets  # noqa: E402
from flowkit.model import load_yaml, resolve_inputs  # noqa: E402
from flowkit.store import FlowStore  # noqa: E402

FLOW = ROOT / "examples" / "flows" / "git-push-recovery"

# scenario -> (final status, a node the run must pass through, how to answer pauses)
EXPECT = {
    "happy":    ("done", "push", {}),
    "behind":   ("done", "pull_rebase", {}),                    # remote ahead: rebase, never force
    "conflict": ("needs_human", "rebase_abort", {}),            # rebase conflicts: abort, hand to a person
    "https":    ("done", "set_ssh", {"needs_confirm": "--approve"}),
    "flaky":    ("done", "wait_retry", {}),                     # "Connection refused": wait and retry
    "flaky2":   ("needs_claude", "push", {}),                   # unrecognised network error, no model: Claude
    "hook":     ("needs_claude", "push", {}),                   # server hook rejects: no path fits
}


@pytest.mark.parametrize("scenario", list(EXPECT))
def test_git_push_recovery(scenario, tmp_path, monkeypatch):
    work = Path(subprocess.run([str(ROOT / "tests" / "sandboxes" / "git_repo.sh"), scenario, str(tmp_path / "sb")],
                               capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1])
    (work / ".git" / "info" / "exclude").write_text(".claude/\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(work))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for v in ("DECISION_MODEL_URL", "DECIDER_URL", "JEV_URL"):
        monkeypatch.delenv(v, raising=False)
    secrets._cache.clear()
    s = FlowStore("git-push-recovery")
    shutil.copytree(FLOW / "steps", s.dir / "steps")
    assert s.apply([{"op": "replace_graph", "graph": load_yaml((FLOW / "flow.yaml").read_text())}], "t", "c")["status"] == "applied"

    want, via, answers = EXPECT[scenario]
    g = s.load()
    res = engine.run(s, s.new_run(g["version"], resolve_inputs(g, {}), g["start"]))
    while res["status"] in answers:
        res = engine.resume(s, s.load_run(res["run"]), [answers[res["status"]]])
    assert res["status"] == want, res
    assert via in [e["node"] for e in res["steps"]]
    if want == "done":  # the remote really has the local commit, and nothing was force-pushed
        log = subprocess.run(["git", "-C", str(work), "log", "--format=%s", "origin/main"], capture_output=True, text=True).stdout
        assert "local work" in log and ("other: README.md" in log or scenario not in ("behind",))
