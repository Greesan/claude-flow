"""Shared command/trace helpers: scrubbing, step splitting, step naming, diagnostic-vs-action labels.

Ported from parse_sessions.py so the live run recorder (observe hook) and the session miner name
and clean steps the same way. Heuristics only; nothing here calls a model.
"""
import re
import shlex

# ---------------------------------------------------------------- scrubbing

SECRETS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
]
KV_SECRET = re.compile(r"(?i)\b(\w*(?:api[_-]?key|secret|token|passw(?:or)?d)\w*)\b(\s*[=:]\s*)\S+")
BEARER = re.compile(r"(?i)\b(bearer|token)(\s+)[A-Za-z0-9._\-+/=]{20,}")
# printf/echo of a literal piped into a command that takes a secret on stdin
PIPED_SECRET = re.compile(
    r"""(?i)\b(printf|echo)\s+(?:-\w+\s+)*(['"]).*?\2(?=\s*\|[^|;&]*?(?:secret\s+put|password-stdin|with-token))""",
    re.S,
)
# quoted token-like literals: 24+ chars, no spaces, has a letter and a digit
QUOTED = re.compile(r"""(['"])([^'"\s]{24,})\1""")
# bare high-entropy strings: 32+ chars with upper, lower and a digit, not path-like. Over-redacts a little.
BARE = re.compile(
    r"(?<![\w/.\-~=])(?=[A-Za-z0-9+/_\-]*\d)(?=[A-Za-z0-9+/_\-]*[a-z])(?=[A-Za-z0-9+/_\-]*[A-Z])"
    r"[A-Za-z0-9][A-Za-z0-9+/_\-]{30,}={0,2}(?![\w])"
)


def _quoted(m):
    v = m.group(2)
    if re.match(r"[/~.]|https?:", v) or re.fullmatch(r"[0-9a-fA-F-]{36}", v):
        return m.group(0)
    if re.search(r"\d", v) and re.search(r"[A-Za-z]", v):
        return f"{m.group(1)}[REDACTED]{m.group(1)}"
    return m.group(0)


def _bare(m):
    v = m.group(0)
    return v if re.fullmatch(r"[0-9a-fA-F-]{36}", v) else "[REDACTED]"  # keep UUIDs (session ids)


def scrub(s):
    """Best-effort secret removal. Not a guarantee."""
    if not isinstance(s, str):
        return s
    for p in SECRETS:
        s = p.sub("[REDACTED]", s)
    s = PIPED_SECRET.sub(lambda m: f"{m.group(1)} [REDACTED]", s)
    s = BEARER.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", s)
    s = QUOTED.sub(_quoted, s)
    s = BARE.sub(_bare, s)
    return KV_SECRET.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", s)


def has_secret(s):
    return isinstance(s, str) and scrub(s) != s


def squash(s, head=500, tail=500):
    """Scrub, then cut the middle (not the end) of long text."""
    s = scrub(s or "")
    if len(s) <= head + tail:
        return s
    cut = len(s) - head - tail
    return f"{s[:head]}\n[... {cut} chars cut ...]\n{s[len(s) - tail:] if tail else ''}"


PATH_RE = re.compile(
    r"(?<![\w/.$-])((?:\$\{?\w+\}?|~|\.{1,2})?/?(?:[\w.@+-]+/)+[\w.@+-]+"
    r"|[\w.-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|md|json|ya?ml|toml|sql|sh|html|css|txt|ipynb|baml))\b"
)
NOISE_PATHS = {"/dev/null", "/dev/stdout", "/dev/stderr"}


def bash_paths_re(cmd):
    """File paths mentioned in a (scrubbed) command."""
    out = []
    for m in PATH_RE.finditer(cmd or ""):
        p = m.group(1).rstrip(".,;:)")
        if p and p not in NOISE_PATHS:
            out.append(p)
    return out


# ------------------------------------------------------- steps and step names

MULTI = {"npm", "npx", "yarn", "pnpm", "git", "docker", "kubectl", "gh", "wrangler", "cargo",
         "go", "pip", "pip3", "uv", "make", "systemctl", "claude", "gcloud", "aws",
         "terraform", "brew", "apt", "doppler", "journalctl", "flow"}
WRAPPERS = {"sudo", "time", "env", "nohup", "command"}
# pipeline stages that only reshape output or feed a value in; the real step is elsewhere
FILTERS = {"tail", "head", "grep", "egrep", "fgrep", "awk", "sed", "sort", "uniq", "wc",
           "cut", "tr", "tee", "less", "more", "jq"}
PRODUCERS = {"printf", "echo", "cat", "yes"}
NOISE_PROGS = {"cd", "export", "set", "source", ".", "echo", "printf", "true", "false", ":",
               "sleep", "pwd", "clear", "unset", "test", "[", "[[", "for", "while", "until", "if", "then", "else",
               "elif", "fi", "do", "done", "case", "esac", "{", "}", "(", ")", "function", "return", "exit", "local"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\b", re.S)


def _stage_prog(stage):
    toks = stage.split()
    while toks and (re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]) or toks[0] in WRAPPERS):
        toks = toks[1:]
    return toks[0].rsplit("/", 1)[-1] if toks else ""


def _split_pipe(seg):
    parts, cur, q = [], [], None
    for ch in seg:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            cur.append(ch)
        elif ch == "|":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def split_steps(cmd):
    """[(segment, piped)] split on && || ; newline outside quotes. Within a pipeline, filters
    (tail/grep/...) and value producers (printf/echo/cat) are dropped and the real step kept.
    piped=True only when the pipe's LAST stage was dropped: then the exit status belongs to the
    filter (e.g. `npm test | tail`) and can hide a real failure."""
    segs, cur, q, i = [], [], None, 0

    def flush():
        t = "".join(cur).strip()
        cur.clear()
        if t:
            segs.append(t)

    while i < len(cmd):
        ch = cmd[i]
        if q:
            cur.append(ch)
            if ch == q and cmd[i - 1] != "\\":
                q = None
        elif ch in "'\"":
            q = ch
            cur.append(ch)
        elif cmd.startswith("&&", i) or cmd.startswith("||", i):
            flush()
            i += 1
        elif ch in ";\n":
            flush()
        else:
            cur.append(ch)
        i += 1
    flush()
    out = []
    for seg in segs:
        stages = _split_pipe(seg)
        if len(stages) == 1:
            out.append((stages[0].strip(), False))
            continue
        progs = [_stage_prog(s) for s in stages]
        keep = [i for i, p in enumerate(progs) if p not in FILTERS and p not in PRODUCERS] or [0]
        last_kept = keep[-1] == len(stages) - 1
        for i in keep:
            out.append((stages[i].strip(), not last_kept))
    return out


def cmd_key(seg, env=None):
    """Canonical name of a step: program + subcommand words (+ one short flag).
    `npx wrangler secret put X` -> 'npx wrangler secret put'. None for noise (cd, echo, comments)."""
    try:
        toks = shlex.split(seg, posix=True)
    except ValueError:
        toks = seg.split()
    while toks and (re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]) or toks[0] in WRAPPERS):
        toks = toks[1:]
    if not toks or toks[0].startswith("#"):
        return None
    m = re.fullmatch(r"\$\{?(\w+)\}?", toks[0])
    if m and env and m.group(1) in env:
        toks[0] = env[m.group(1)]
    if toks[0] in NOISE_PROGS:
        return None
    prog = toks[0].rsplit("/", 1)[-1]
    words, rest = [prog], toks[1:]
    if prog in MULTI:
        n = 0
        while rest and n < 3 and re.fullmatch(r"[a-z][a-z0-9_-]*", rest[0]):
            words.append(rest.pop(0))
            n += 1
    if rest and rest[0].startswith("-") and len(rest[0]) <= 12 and "=" not in rest[0]:
        words.append(rest[0])
    return " ".join(words)


def strip_heredocs(cmd):
    """`python3 - <<'PY' ... PY` -> `python3 -`: a heredoc body is data, not steps."""
    return HEREDOC.sub("", cmd or "")


def cmd_keys(cmd):
    steps = split_steps(scrub(strip_heredocs(cmd)))
    env = {}
    for seg, _ in steps:  # simple assignments like B="/path/to/tool" used later as $B
        m = re.fullmatch(r"([A-Za-z_]\w*)=(.+)", seg.strip(), re.S)
        if m:
            env[m.group(1)] = m.group(2).strip("'\"")
    return [k for k in (cmd_key(seg, env) for seg, _ in steps) if k], any(p for _, p in steps)


def norm_cmd(cmd):
    c = re.sub(r"\s*2>&1", "", scrub(cmd or ""))
    c = re.sub(r"/home/[^/\s'\"]+", "~", c)
    c = re.sub(r"\b[0-9a-f]{20,}\b", "<id>", c)
    return re.sub(r"\s+", " ", c).strip()[:200]


# ------------------------------------------------------- diagnostic vs action

# Step names that only look at things. A command is a *diagnostic* when every step in it is one of
# these; diagnostics become checks and decide-node conditions, actions become steps.
READ_PROGS = {"ls", "cat", "head", "tail", "grep", "egrep", "rg", "find", "pwd", "which", "type", "wc", "stat",
              "file", "du", "df", "ps", "env", "printenv", "id", "whoami", "hostname", "uname", "date", "ss",
              "netstat", "lsof", "ping", "dig", "nslookup", "host", "realpath", "readlink", "diff", "jq", "tree",
              "wslpath", "nproc", "free", "uptime", "less", "more", "sha256sum", "md5sum"}
READ_KEYS = re.compile(
    r"^(git (status|log|diff|show|branch|remote|rev-parse|ls-files|config( --get| -l)?|fetch --dry-run|blame|tag)"
    r"|(sudo )?(systemctl (status|is-active|is-enabled|list-units|show)|journalctl)"
    r"|docker (ps|logs|inspect|images|version)|kubectl (get|describe|logs|top|version|config)"
    r"|npm (ls|view|outdated|config get|run --list)|npx wrangler (whoami|deployments list|tail)"
    r"|gh (auth status|pr (view|list|status|checks)|run (view|list)|repo view|api)"
    r"|uv (pip list|pip show|tree|python list)|pip3? (list|show|freeze)|doppler (configure|me|setup --help)"
    r"|curl( -[sSIfL]+)*|wget --spider|flow (list|status|runs|show|validate|export|history|candidates))\b")


def is_diagnostic(cmd):
    keys, _ = cmd_keys(cmd)
    if not keys:
        return True  # cd/echo/sleep only
    if re.search(r"(^|[^2&])>(?!&)|\btee\b|\bsed\s+-i|\s-delete\b|\s-exec\b", cmd or ""):
        return False  # writes a file
    if re.search(r"\bcurl\b.*(-X\s*(POST|PUT|PATCH|DELETE)|\s(-d|--data\S*|-F|--form|-T|--upload-file)\s)", cmd or ""):
        return False  # sends something
    return all(k.split()[0] in READ_PROGS or READ_KEYS.match(k) or _probe(k) for k in keys)


def _probe(key):
    """`tool --version`, `tool --help`, plain `sed` (prints unless -i): looking, not doing."""
    w = key.split()
    return w[-1] in ("--version", "-version", "--help", "-h", "-V", "version") or (w[0] == "sed" and "-i" not in w)


def is_flow_call(cmd):
    """The runner itself (`uv run …/flow.py …`, `flow …`): never part of what a flow learns."""
    return bool(re.search(r"(^|[\s/])flow(\.py)?\s+(run|resume|learn|status|runs|list|show|export|validate|"
                          r"history|candidates|mine|draft|node|edge|recover|input|meta|commit|import|revert|"
                          r"proposals|accept|reject|new|observe)\b", cmd or ""))
