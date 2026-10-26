# Environment for a local, loopback-only laya-serve. Source before running ~/venvs/laya/bin/laya-serve.
export LAYA_HOST=127.0.0.1
export LAYA_PORT=8765
export LAYA_DEFAULT_MODEL=english
export LAYA_PRELOAD=1
export LAYA_MODELS=typed-decisions   # clients must send "model": "typed-decisions"
export LAYA_THREADS=6
export LAYA_REVISION=reviewed
export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
