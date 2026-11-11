"""Per-user macOS LaunchAgent lifecycle. Generating/validating a plist never installs it."""

from __future__ import annotations

import os
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

LABEL = "com.exergiclabs.gtm.grok-import"
LAUNCHCTL = "/bin/launchctl"


def build_plist(project: Path, logs: Path, *, python=None):
    # Keep the venv path: resolving its symlink would select the base Python without dependencies.
    executable = Path(python or sys.executable).absolute()
    return {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "-m", "gtm.cli", "import", "grok", "--all"],
        "WorkingDirectory": str(project.resolve()),
        "StartInterval": 900,
        "RunAtLoad": True,
        "StandardOutPath": str(logs.absolute() / "grok-import.stdout.log"),
        "StandardErrorPath": str(logs.absolute() / "grok-import.stderr.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "Umask": 0o077,
    }


def validate_plist(spec):
    if (
        spec.get("Label") != LABEL
        or spec.get("StartInterval") != 900
        or spec.get("RunAtLoad") is not True
    ):
        raise ValueError("invalid scheduler label or cadence")
    args = spec.get("ProgramArguments", [])
    if len(args) != 6 or args[1:] != ["-m", "gtm.cli", "import", "grok", "--all"]:
        raise ValueError("scheduler must invoke Python directly with import grok --all")
    for path in (
        args[0],
        spec.get("WorkingDirectory", ""),
        spec.get("StandardOutPath", ""),
        spec.get("StandardErrorPath", ""),
    ):
        if not Path(path).is_absolute():
            raise ValueError(
                "scheduler executable, working directory, and logs must use absolute paths"
            )
    if set(spec.get("EnvironmentVariables", {})) != {"PYTHONUNBUFFERED"}:
        raise ValueError(
            "scheduler plist must not contain credentials or inherited environment variables"
        )
    return plistlib.dumps(spec, fmt=plistlib.FMT_XML, sort_keys=True)


class MacScheduler:
    def __init__(self, project, logs, *, home=None, python=None, runner=subprocess.run):
        self.project, self.logs = Path(project).resolve(), Path(logs).absolute()
        self.spec = build_plist(self.project, self.logs, python=python)
        self.path = Path(home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        self.domain = f"gui/{os.getuid()}"
        self.target = f"{self.domain}/{LABEL}"
        self.runner = runner

    def command(self, *args):
        try:
            return self.runner(
                [LAUNCHCTL, *args], capture_output=True, text=True, check=False, timeout=15
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "launchctl timed out; inspect scheduler status before retrying"
            ) from exc

    def status(self):
        installed = self.path.is_file()
        result = {
            "label": LABEL,
            "plist": str(self.path),
            "installed": installed,
            "loaded": False,
            "automatic_ingestion_enabled": False,
            "interval_seconds": 900,
            "stdout": self.spec["StandardOutPath"],
            "stderr": self.spec["StandardErrorPath"],
        }
        if platform.system() != "Darwin":
            return {**result, "platform": "unsupported; macOS required"}
        status = self.command("print", self.target)
        disabled = self.command("print-disabled", self.domain)
        match = re.search(r'"' + re.escape(LABEL) + r'"\s*=>\s*(true|false)', disabled.stdout)
        is_disabled = bool(match and match.group(1) == "true")
        loaded = status.returncode == 0
        result.update(
            loaded=loaded,
            disabled=is_disabled,
            automatic_ingestion_enabled=installed and loaded and not is_disabled,
        )
        if loaded:
            for name, pattern in (
                ("state", r"\bstate = ([^\n]+)"),
                ("last_exit_code", r"\blast exit code = (\d+)"),
            ):
                found = re.search(pattern, status.stdout)
                if found:
                    result[name] = found.group(1).strip()
        elif status.stderr:
            result["launchctl_status"] = status.stderr.strip()
        if disabled.returncode:
            result["disabled_state_unknown"] = True
            result["automatic_ingestion_enabled"] = False
        return result

    def install(self):
        if platform.system() != "Darwin":
            raise ValueError("LaunchAgent installation requires macOS")
        if os.getuid() == 0:
            raise ValueError("Run scheduler install as your normal macOS user, without sudo")
        content = validate_plist(self.spec)
        if not (self.project / "pyproject.toml").is_file() or not os.access(
            self.spec["ProgramArguments"][0], os.X_OK
        ):
            raise ValueError(
                "run from the installed GTM project using its virtual environment Python"
            )
        if self.path.exists() and plistlib.loads(self.path.read_bytes()) != self.spec:
            raise ValueError(
                "an existing scheduler has different paths/settings; uninstall it before reinstalling"
            )
        status = self.status()
        if status.get("disabled"):
            raise ValueError(
                f"LaunchAgent is disabled; run launchctl enable {self.target}, then install again"
            )
        if status["automatic_ingestion_enabled"]:
            return status
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        for log in ("grok-import.stdout.log", "grok-import.stderr.log"):
            fd = os.open(
                self.logs / log, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
            os.fchmod(fd, 0o600)
            os.close(fd)
        existed = self.path.exists()
        fd, tmp = tempfile.mkstemp(prefix=f".{LABEL}-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
            result = self.command("bootstrap", self.domain, str(self.path))
            if result.returncode:
                if not existed:
                    self.path.unlink(missing_ok=True)
                raise RuntimeError(
                    f"launchctl bootstrap failed: {result.stderr.strip()}; check scheduler status and macOS Background Items"
                )
        finally:
            Path(tmp).unlink(missing_ok=True)
        return self.status()

    def uninstall(self):
        if platform.system() != "Darwin":
            raise ValueError("LaunchAgent removal requires macOS")
        if os.getuid() == 0:
            raise ValueError("Run scheduler uninstall as your normal macOS user, without sudo")
        current = self.status()
        if current["loaded"]:
            result = self.command("bootout", self.target)
            if result.returncode:
                raise RuntimeError(
                    f"launchctl bootout failed: {result.stderr.strip()}; plist retained"
                )
        self.path.unlink(missing_ok=True)
        return self.status()
