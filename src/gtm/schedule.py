"""Run ``gtm pipeline`` every day on this Mac with launchd.

launchd runs the job at ``pipeline.run_at`` (local time). If the Mac is asleep then, it
runs when the Mac wakes. Output goes to ``data/logs/pipeline.log``; the run itself, with
each step's outcome, is recorded in ops.pipeline_runs.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

LABEL = "com.exergiclabs.gtm.pipeline"


def parse_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value).strip())
    if not match:
        raise ValueError(f"pipeline.run_at must be HH:MM (24-hour), got {value!r}")
    return int(match.group(1)), int(match.group(2))


def tool_path() -> str:
    """PATH for the job: launchd starts with a bare PATH, so include where uv and the
    deepline CLI live."""
    dirs = []
    for tool in ("uv", "deepline"):
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys(dirs))


def build_plist(project: Path, run_at: str, *, uv: str | None = None) -> dict:
    hour, minute = parse_time(run_at)
    uv = uv or shutil.which("uv")
    if not uv:
        raise RuntimeError("uv not found on PATH")
    logs = project / "data" / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            uv,
            "run",
            "--quiet",
            "--directory",
            str(project),
            "gtm",
            "pipeline",
        ],  # fmt: skip
        "WorkingDirectory": str(project),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": {"PATH": tool_path()},
        "StandardOutPath": str(logs / "pipeline.log"),
        "StandardErrorPath": str(logs / "pipeline.log"),
        "RunAtLoad": False,
    }


class Schedule:
    def __init__(self, project: Path, *, home: Path | None = None, runner=subprocess.run):
        self.project = project.resolve()
        self.home = home or Path.home()
        self.runner = runner
        self.plist_path = self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def _launchctl(self, *args) -> subprocess.CompletedProcess:
        return self.runner(["launchctl", *args], capture_output=True, text=True)

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    def install(self, run_at: str, *, dry_run: bool = False) -> dict:
        spec = build_plist(self.project, run_at)
        if dry_run:
            return {"dry_run": True, "plist": str(self.plist_path), "spec": spec}
        (self.project / "data" / "logs").mkdir(parents=True, exist_ok=True)
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self._launchctl("bootout", f"{self.domain}/{LABEL}")  # replace an older version
        self.plist_path.write_bytes(plistlib.dumps(spec))
        done = self._launchctl("bootstrap", self.domain, str(self.plist_path))
        if done.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {done.stderr.strip()}")
        return {"installed": True, "plist": str(self.plist_path), "runs_daily_at": run_at,
                "log": spec["StandardOutPath"]}  # fmt: skip

    def status(self) -> dict:
        if not self.plist_path.exists():
            return {"installed": False}
        spec = plistlib.loads(self.plist_path.read_bytes())
        when = spec.get("StartCalendarInterval", {})
        done = self._launchctl("print", f"{self.domain}/{LABEL}")
        last_exit = re.search(r"last exit code = (\S+)", done.stdout or "")
        return {"installed": True, "loaded": done.returncode == 0,
                "runs_daily_at": f"{when.get('Hour', 0):02d}:{when.get('Minute', 0):02d}",
                "last_exit_code": last_exit.group(1) if last_exit else None,
                "log": spec.get("StandardOutPath")}  # fmt: skip

    def uninstall(self) -> dict:
        self._launchctl("bootout", f"{self.domain}/{LABEL}")
        existed = self.plist_path.exists()
        if existed:
            self.plist_path.unlink()
        return {"uninstalled": existed}
