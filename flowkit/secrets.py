"""Secrets for flow steps, resolved from Doppler at run time.

Flows name secrets (`secrets: [CLOUDFLARE_API_TOKEN]`); values are fetched per run, injected into the
one step that declared them, masked out of everything that step prints, and never written to state,
logs, events or the flow. Claude only ever sees the names.

Doppler scope: `secrets_from: {doppler: {project: <p>, config: <c>}}` in the flow, or whatever
`doppler setup` configured for the step's directory.
"""
import subprocess


class SecretError(Exception):
    pass


_cache = {}


def resolve(names, flow, cwd):
    src = (flow.get("secrets_from") or {}).get("doppler") or {}
    values = {}
    for name in names:
        key = (name, src.get("project"), src.get("config"), str(cwd))
        if key not in _cache:
            cmd = ["doppler", "secrets", "get", name, "--plain"]
            if src.get("project"):
                cmd += ["--project", src["project"]]
            if src.get("config"):
                cmd += ["--config", src["config"]]
            try:
                r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise SecretError(f"doppler could not be run for {name}: {type(e).__name__}")
            if r.returncode != 0 or not r.stdout.strip():
                why = (r.stderr.strip().splitlines() or ["empty value"])[-1][:200]
                raise SecretError(f"doppler did not return {name} ({why}). Check `doppler login`, the project/config, "
                                  f"and that the secret exists: `doppler secrets get {name} --plain` (run it yourself).")
            _cache[key] = r.stdout.rstrip("\n")
        values[name] = _cache[key]
    return values


def mask(text, values):
    """Replace every exact secret value with <secret:NAME>."""
    for name, v in sorted(values.items(), key=lambda kv: -len(kv[1])):
        if len(v) >= 4:
            text = text.replace(v, f"<secret:{name}>")
    return text
