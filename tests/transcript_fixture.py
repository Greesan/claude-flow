"""Write synthetic Claude Code transcripts (the record shapes seen in real ~/.claude/projects files)."""
import json
import secrets
from pathlib import Path

from flowkit import transcripts


class Transcript:
    def __init__(self, project_dir, session="sess-1"):
        self.dir = transcripts.PROJECTS / transcripts.project_folder(project_dir)
        self.path = self.dir / f"{session}.jsonl"
        self.recs, self.n = [], 0

    def _ts(self):
        self.n += 1
        return f"2026-10-26T10:{self.n // 60:02d}:{self.n % 60:02d}Z"

    def tool(self, name, inp, result, ok=True):
        self.n += 1
        tid = "toolu_" + secrets.token_hex(8)  # unique across files, like real ids
        ts = self._ts()
        self.recs.append({"type": "assistant", "timestamp": ts, "cwd": "/home/u/repo",
                          "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        self.recs.append({"type": "user", "timestamp": ts, "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result if ok else f"Exit code 1\n{result}",
             "is_error": not ok}]}})
        return self

    def flow(self, cmd, res):
        """A Bash call to flow whose output is the flow's JSON result (how a pause shows up)."""
        return self.tool("Bash", {"command": cmd}, json.dumps(res))

    def bash(self, cmd, out="", ok=True):
        return self.tool("Bash", {"command": cmd}, out, ok)

    def user(self, text):
        self.recs.append({"type": "user", "timestamp": self._ts(), "message": {"content": text}})
        return self

    def ask(self, question, answer):
        return self.tool("AskUserQuestion", {"questions": [{"question": question}]},
                         f'User has answered your questions: "{question}"="{answer}". You can now continue.')

    def compact(self):
        self.recs.append({"type": "system", "subtype": "compact_boundary", "timestamp": self._ts(),
                          "compactMetadata": {"trigger": "auto"}})
        self.recs.append({"type": "user", "isCompactSummary": True, "timestamp": self._ts(),
                          "message": {"content": "This session is being continued from a previous conversation..."}})
        return self

    def agent(self, agent_id, sub):
        """An Agent call whose subagent transcript is `sub` (another Transcript, written to subagents/)."""
        sub.path = self.dir / self.path.stem / "subagents" / f"agent-{agent_id}.jsonl"
        sub.write()
        return self.tool("Agent", {"description": "fix it", "prompt": "..."},
                         f"Async agent launched successfully.\nagentId: {agent_id} (internal ID)")

    def write(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.path).write_text("".join(json.dumps(r) + "\n" for r in self.recs))
        return self.path
