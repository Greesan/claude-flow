# Laya: the optional decision model

How a local Laya server was set up, measured and calibrated as `flow`'s decision model. `flow` works without it.

## What runs
- **Laya 0.4.14** in `~/venvs/laya` (Python 3.13, CPU torch 2.14.1). Source: PyPI + canonical HF `convaiinnovations/laya`, pinned to reviewed revision `55cf4c4`.
- **Decision checkpoint: `typed-decisions`**, the fine-tuned one. The English base (what `Router()` uses by default) gave the same accuracy but much worse confidence.
- **Server:** `laya-serve` on `127.0.0.1:8765` only. Jev-shaped `POST /v1/systemone`. Contract in `examples/`.
- **Network:** fully offline after download (`HF_HUB_OFFLINE=1`). No outbound connections observed.

## Latency (Intel Core 7 150U laptop, 6 threads)
| | |
|---|---|
| One 3-option choice, warm, idle machine | p50 ~0.55 s (English), ~0.6 s over HTTP (typed-decisions) |
| Under sustained load or with other apps running | ~1–1.5 s |
| Long input (~1,500 chars) | ~4 s |
| Server cold start | ~9 s |
| Server RAM | ~2.0 GB |

Fast enough for one decision per flow step, but not for hot loops. Keep the server running; don't spawn a process per call.

## Accuracy on 35 domain cases (`eval/`)
- **Answerable cases:** 21/28 correct (75%).
- **No "none of the above".**
  - An explicit "none" option hurts: answerable accuracy drops to 64%.
  - Express abstention with the confidence gate instead.
- **Prompt injection:** 2 of 3 adversarial cases followed the injected answer, though at low confidence (0.40–0.55).
- **Long inputs:**
  - Laya keeps the *start* of the input and drops the rest.
  - The adapter must cut to the **last ~400 tokens** (by tokens, not characters) and collapse noise lines such as progress bars.
  - Check `usage.truncated == false` before trusting an answer.
- **Deterministic:** the same input gives bit-identical outputs on CPU. GPU (bf16) would shift some answers. Thresholds are CPU-fitted.

## Threshold (`eval/calibration.json`)
**Act when `answers.<qid>.answer_confidence ≥ 0.70`; otherwise fall back to Claude.**
- **Field:** use `answer_confidence`, never `confidence`. `confidence` is entropy-based and doesn't mean what Jev's does.
- **Expected:** acts on ~30% of failures and is right ~75–90% of the time (2-fold held-out vs. the full set).
- **The runner's 0.85 never fires** on this checkpoint.
- **Run regex matchers before Laya.** One leak at 0.70 is a merge conflict scored as a test failure; a regex catches it first.
- **Temperature refit:** worth about +11 points held-out, but the server can't load calibration files and n is tiny. Revisit after ~200 logged decisions.

## Start / stop
```bash
cd laya
. ./laya_env.sh && ~/venvs/laya/bin/laya-serve      # foreground; Ctrl-C stops (laya_env.sh picks port 8765)
curl -s 127.0.0.1:8765/health                       # {"status":"ok","loaded":["typed-decisions"],...}
```
- **Clients must send `"model": "typed-decisions"`.** Without it, English text routes to the English base, which loads a second ~2 GB model.
- **Stop a background server:** `pkill -f laya-serve`.

## Uninstall
| What | Path | Size |
|---|---|---|
| venv | `~/venvs/laya` | 993 MB |
| Model weights (English + typed-decisions) | `~/.cache/huggingface/hub/models--convaiinnovations--laya` | 1.6 GB |
| uv download cache (shared with other projects) | `~/.cache/uv` | prune with `uv cache prune` |

```bash
rm -rf ~/venvs/laya ~/.cache/huggingface/hub/models--convaiinnovations--laya
```

## Files
- `laya_env.sh`: server environment.
- `examples/`: request and response contract, full and strict.
- `eval/`:
  - `build_cases.py` → `cases.jsonl`
  - `run_eval.py --ckpt …`
  - `report.py`
  - `calibrate.py` → `calibration.json`

## Using it from flow
Point flow at the server (use your own host and port; `laya-serve` listens on 8000 unless `LAYA_PORT` says otherwise):
```bash
export DECISION_MODEL_URL=http://127.0.0.1:8765/v1/systemone
export DECISION_MODEL_NAME=typed-decisions
```
flow sends the request shape in `examples/`, reads `answer_confidence`, acts only at 0.70 or above (`DECISION_MODEL_THRESHOLD`),
ignores truncated input, gives up after 5 s (`DECISION_MODEL_TIMEOUT`) and logs every decision for refitting.
