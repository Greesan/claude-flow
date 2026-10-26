# claude-flow

Saved procedures for Claude Code.

**Who it's for:** Claude Code users who keep walking the agent through the same multi-step procedures (a deploy, a release, an app build, a server restart) and keep correcting it along the way. You write the procedure down once as a **flow**. Claude then runs the flow instead of working the steps out turn by turn, and it does the same thing every time. You can read, chart and version each flow, and the risky steps wait for your approval. Claude is only brought in when a flow hits a failure it hasn't seen before.

It runs locally and isn't a CI/CD system.

Measured on three real tasks (a push to a remote that was ahead, an Android app build, and a two-service deploy), compared with Claude doing them by hand: about half the time to finish, 2 model calls instead of 4–8, and 1.2–1.6× lower token cost, with the same result.

## Install

Needs [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Greesan/claude-flow && cd claude-flow
uv tool install --editable .     # puts `flow` on your PATH
flow hooks                       # prints the Claude Code hook settings to merge into ~/.claude/settings.json
```

## Run a flow

```bash
flow import git-push-recovery examples/flows/git-push-recovery/flow.yaml --global   # install the example
flow list                                   # flows for this project and your global ones, with inputs and average time
flow run git-push-recovery                  # prints one JSON result
flow run git-push-recovery -i remote=upstream
flow resume git-push-recovery --run <id> --approve
flow export git-push-recovery --out chart.mmd                                           # Mermaid flowchart
```

A run ends `done`, or pauses with one of these:

| Pause | Meaning | Continue with |
|---|---|---|
| `needs_confirm` | a step that changes something shared is waiting for your OK | `--approve` (only after you say yes) |
| `needs_human` | you need to do something first, e.g. log in somewhere | `--done` |
| `needs_claude` | a failure no recovery path covers; Claude picks one or handles it | `--choose <id>` |
| `failed` | no recovery path at all; Claude fixes it and can teach the flow (below) | `--done` to retry |

Slow flows (builds, deploys) should be started with Claude's background Bash, so the 2-minute tool timeout can't cut a step off.

## Write a flow

Flows live in `.claude/flows/<name>/` (one project) or `~/.claude/flows/<name>/` (every project):

```yaml
about: Push, recovering from the usual failures.
inputs:
  remote: {type: string, default: origin}
start: push
nodes:
  push:
    type: run
    cmd: git push {{ inputs.remote }}
    effect: idempotent
    timeout: 120
    ok: done
    recover:
      - id: remote_ahead
        when: the remote has commits we don't have; pull with rebase, then push again
        match: rejected.*\((fetch first|non-fast-forward)\)
        to: pull_rebase
  pull_rebase: {type: run, cmd: "git pull --rebase {{ inputs.remote }}", effect: idempotent, ok: push}
  done: {type: end, say: "Pushed. {{ last_output }}"}
```

**Steps** (`type`):
- `run` runs a shell command (`cmd`) or a script in `steps/` (`script`).
- `decide` branches on a `check` command: exit 0 takes the first option, anything else the second.
- `confirm` waits for your approval, `human` waits for you to do something, `end` finishes.

**`effect`** is required on every `run` step and says how risky it is:

| `effect` | Use it for | What flow does |
|---|---|---|
| `read` | steps that only look (status, logs) | runs it freely |
| `idempotent` | steps where running twice is harmless (pushing the same commits, deploying the same code) | runs it freely and lets recovery repeat it |
| `once` | steps that must not repeat without a person agreeing (changing a remote, restarting a server) | only allows it right after a `confirm` step |

**Other step settings:** `env`, `timeout`, `secrets`, `outputs` and `check`.
- `secrets` names values fetched from Doppler, given to that step only and masked out of all output.
- `outputs` names values the step reports, for later steps and the final message.
- `check` is a command that exits 0 once the step's effect is in place. The step is skipped if the check already passes, and fails if the command exits 0 but the check still doesn't pass.

**Recovery paths** (`recover`) say what to do when a step fails:
- `id` is the path's name. It's used in logs and charts, in the success and failure counts, and with `--choose <id>`.
- `when` describes the situation in plain words. Claude reads it when a flow hands a failure over, and so does the optional decision model.
- `match` is a regex for recognising the failure from the step's output.
- `to` is the step to go to.

**What happens when a step fails,** in order:
1. the first recovery path whose `match` fits the output
2. the decision model, if you run one
3. the step's `fail` edge
4. Claude

**Editing.** Change a flow by editing `flow.yaml` and running `flow commit`, or with `flow node|edge|recover|input …`.
- Every change is checked before it's saved, and is saved as a new version. `flow history`, `flow diff` and `flow revert --to N` work on these versions.
- A run that's already going keeps the version it started with.
- When two runs change the same flow at once, additions merge automatically. Changes that clash go to `flow proposals`, where you accept or reject them.

**Timing.** Every step records how long it took. `flow list`, `flow show` and the chart labels give the average and p90 for each step and for a whole run.

## Teach a flow from what Claude did

When a run pauses on something new, let Claude fix it, then:

```bash
flow learn <flow> --run <id> --propose        # the failure plus the commands Claude ran, read from the session log
flow learn <flow> --run <id> --patch p.json   # Claude's change to the flow, checked before it's added
```

**Each change is checked before it's added:**
- It must point to the commands that justify it.
- Every new step needs a `check` that passes right now.
- The step that failed must succeed again.

**After it's added:**
- A new recovery path sits behind an approval step until it has worked 3 times. It's removed after 2 failures.
- Learned steps that turn out never to be needed are removed automatically.

## Find procedures worth saving

```bash
flow mine                        # reads your local Claude Code session logs; no model calls, nothing leaves the machine
flow candidates                  # repeated command sequences, and failures you had to talk Claude through
flow draft <id> --as <name>      # gives Claude the evidence to write the flow
flow usage                       # how often each flow ran, from the main session vs subagents
```

## Hooks

`flow hooks` prints these. All run locally.

| Hook | What it does |
|---|---|
| Session start | Tells Claude which flows exist and which runs are paused, so it uses them, including after context compaction |
| Before compaction | Saves what Claude did on paused runs, and updates the candidate list |
| Session end | Updates the candidate list |
| Before each Bash call | Blocks `flow resume … --approve` unless you approved after the flow asked. Plain bash; Python only starts for `--approve` |

## Optional decision model

When no regex matches a failure, a small classification model can pick the recovery path from the `when` descriptions. It acts only when it's at least 70% confident, and hands the failure to Claude otherwise. Point flow at your server with `DECISION_MODEL_URL` (the address your server listens on) and `DECISION_MODEL_NAME`. It speaks the Jev `/v1/systemone` request format, and [`laya/`](laya/) covers running a local [Laya](https://huggingface.co/convaiinnovations/laya) model and how the 70% was chosen. Without it, those failures simply go to Claude.

## Develop

```bash
uv run pytest -q                 # unit tests, plus the example flow against 7 real local git repos
```

```
flowkit/          cli, engine (runs steps), learn, model (format + checks), ops (edits), store (versions, runs),
                  transcripts, mine, hooks, decide (decision model), secrets (Doppler), mermaid, trace
examples/flows/   flows you can import
tests/            pytest suite; sandboxes/ builds throwaway git repos for the scenario tests
laya/             optional local decision model: setup notes, evaluation, calibration
```

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md). Free to use, change and share for any non-commercial purpose: personal
projects, study, research, hobby work, and use by charities, schools and public bodies. Using it to make money (selling
it, or using it in a business) needs a separate license from the author; open an issue to ask. This makes the project
source-available rather than open source.
