# claude-flow

Claude Code learns the procedures you keep walking it through, and runs them the same way every time.

**Who it's for:** Claude Code users who keep taking the agent through the same multi-step procedures (a deploy, a release, an app build, a server restart) and keep correcting it along the way.

**You don't write flows, and you don't tell Claude to use them.** claude-flow reads your past Claude Code sessions. Claude turns the procedures you keep repeating or correcting into **flows**: saved, versioned step-by-step procedures. From then on, Claude runs the matching flow whenever a task calls for it, instead of working the steps out again. Your part is approving the steps that change something shared. You can read, chart or edit any flow, but you don't have to.

It runs locally and isn't a CI/CD system.

Measured on three real tasks (a push to a remote that was ahead, an Android app build, and a two-service deploy), compared with Claude doing them by hand: about half the time to finish, 2 model calls instead of 4–8, and 1.2–1.6× lower token cost, with the same result.

## How it works

1. **Find.** When a session ends, claude-flow scans every transcript that changed, locally and with no model calls, for procedures you repeat and failures you had to talk Claude through. A session that ended without notice (a crash, a closed terminal) gets picked up the next time any session ends.
2. **Write.** At the start of a session, Claude is told about procedures that have gained new evidence since it last heard about them (seen in more sessions, or corrected again). Reopening a session repeats nothing. When there's a natural pause, it drafts a flow from the evidence, tests it, and tells you what it saved.
3. **Use.** Each session starts by telling Claude which flows exist for the project. When a task matches one, Claude runs it in a single call. That stays true after the context gets compacted.
4. **Improve.** If a flow hits a failure it doesn't know, Claude fixes it. Claude then turns that fix into a checked change to the flow, so the next run handles it on its own.

## Install

Needs [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Greesan/claude-flow && cd claude-flow
uv tool install --editable .     # puts `flow` on your PATH
flow hooks                       # prints the Claude Code hook settings to merge into ~/.claude/settings.json
```

After that, there's nothing to do. Optionally, start from the example flow:

```bash
flow import git-push-recovery examples/flows/git-push-recovery/flow.yaml --global
```

## How flows get written

These are the commands Claude uses. You can run them too.

```bash
flow mine                        # scan your local session logs; no model calls, nothing leaves the machine
flow candidates                  # a table of procedures worth a flow, and why (--all adds patterns)
flow draft <id> --as <name>      # the evidence and an outline for writing the flow
flow usage                       # how often each flow ran, from the main session vs subagents
```

A procedure becomes a **candidate** for one or both reasons:

| Reason | What the miner saw |
|---|---|
| `intervention` | You had to correct Claude on a failing step, the fix worked (the step later succeeded, or what Claude did next worked), and it took 2+ extra calls |
| `time_saved` | The same fix worked in 2+ sessions at 2+ extra calls each time, or the same 3+ state-changing steps succeeded in 2+ sessions |

Each candidate also says where the fix goes: a recovery path on the flow that already runs the failing step, or a new flow. Everything else the miner sees is a **pattern**: listed with `--all`, never brought up.

## How flows run

```bash
flow list                        # flows for this project and your global ones, with inputs and average time
flow run <name> -i key=value     # prints one JSON result
flow resume <name> --run <id> --approve
flow export <name> --out chart.mmd                                                   # Mermaid flowchart
```

A finished run returns the flow's declared `outputs` and which `end` step it reached (`ended`). Otherwise it pauses with one of these:

| Pause | Meaning | What happens next |
|---|---|---|
| `needs_confirm` | a step that changes something shared is waiting for your OK | Claude asks you, and continues only after you say yes |
| `needs_human` | you need to do something first, e.g. log in somewhere | you do it, then Claude continues |
| `needs_claude` | a failure no recovery path covers | Claude picks a path or fixes it itself |
| `failed` | no recovery path at all | Claude fixes it, then can teach the flow (below) |

Claude starts slow flows (builds, deploys) in the background, so the 2-minute tool timeout can't cut a step off.

## How flows improve

When a run pauses on something new and Claude fixes it, Claude turns the fix into a change to the flow:

```bash
flow learn <flow> --run <id> --propose        # the failure plus the commands Claude ran, read from the session log
flow learn <flow> --run <id> --patch p.json   # Claude's change to the flow, checked before it's added
```

**Each change is checked before it's added:**
- It must point to the commands that justify it.
- Every new step needs a `check` that passes right now.
- The step that failed must succeed again.

**After it's added:**
- A new recovery path waits for your approval each time it runs, until it has worked 3 times. It's removed after 2 failures.
- Learned steps that turn out never to be needed are removed automatically.

## The flow format

Claude writes these. This section is for when you want to read or adjust one.

Flows live in `.claude/flows/<name>/` (one project) or `~/.claude/flows/<name>/` (every project):

```yaml
about: Push, recovering from the usual failures.
inputs:
  remote: {type: string, default: origin, help: git remote to push to}
outputs:
  ssh_url: {from: set_ssh.url, help: the remote's new SSH URL, when the run switched it}
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

**Inputs and outputs** are the flow's contract, and `flow show` prints them as tables.
- Every input has a `type` (`string`, `enum` with `values`, `int`, `bool`, `path`) and a `help` line. It may also have a `default` and a `pattern`. Inputs without a default are required.
- A step lists what it reports under `outputs`, each with a `help` line. The step reports a value by writing `key=value` lines to `$FLOW_OUTPUT`, or the value is taken from its output with a `regex`. If a step succeeds without reporting a declared value, it fails.
- Later steps can only use `{{ outputs.<step>.<key> }}` for values that step declares. A `decide` step always reports `choice`.
- The top-level `outputs` are what the run returns. Each one is taken `from: <step>.<key>`, and it is `null` when the path the run took skipped that step.

**Steps** (`type`):
- `run` runs a shell command (`cmd`) or a script in `steps/` (`script`).
- `decide` answers a question with a `check` command or a `script`: exit 0 takes the first option, anything else the second. With `by: output`, the last line it prints names the option, so it can choose among more than two.
- `confirm` waits for your approval, `human` waits for you to do something, `end` finishes.
- `flow` runs another saved flow as one step, like a function call (below).

**Flows that run other flows.** A step like `push: {type: flow, flow: git-push-recovery, inputs: {branch: main}, ok: deploy}` runs that flow and continues with its declared `outputs` as `{{ outputs.push.<key> }}`.
- **One copy.** A flow step can call a flow in its own project or a global one (`~/.claude/flows`). A flow that several projects need lives globally, and `flow promote <name>` moves a project flow there. A name that exists both in the project and globally is rejected, so a project copy can never quietly stand in for the shared one.
- **Pinned versions.** The caller records the version it was tested with under `uses:`. Edits and learned fixes to the called flow make new versions, but callers keep their pin until `flow upgrade <caller>` moves it. An upgrade is refused if the new version no longer takes the inputs or returns the outputs the caller uses.
- **Who uses what.** `flow show <name>` lists the flows it calls and the flows that call it, including callers pinned to older versions. Charts draw a flow step as a double-edged box.
- **Pauses and fixes.** If the called flow stops (an approval, something for you to do, a failure), the caller stops with it and you resume the caller. A failure is fixed and learned in the called flow, so every caller gains the fix. A `fail` edge on the flow step catches a called flow that can't continue.

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

## Hooks

`flow hooks` prints these. All run locally.

| Hook | What it does |
|---|---|
| Session start | Tells Claude which flows exist (so it uses them), which runs are paused, and which procedures you repeat still have no flow (so it writes them). Also fires after context compaction |
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
