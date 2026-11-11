import plistlib
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gtm.cli import app
from gtm.scheduler import LABEL, MacScheduler, build_plist, validate_plist


class FakeLaunchctl:
    def __init__(self):
        self.loaded = False
        self.disabled = False
        self.fail_bootstrap = False
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        assert args[0] == "/bin/launchctl"
        if args[1] == "print-disabled":
            text = f'"{LABEL}" => {str(self.disabled).lower()}'
            return subprocess.CompletedProcess(args, 0, text, "")
        if args[1] == "print":
            return subprocess.CompletedProcess(
                args,
                0 if self.loaded else 113,
                "state = not running\nlast exit code = 0\n" if self.loaded else "",
                "" if self.loaded else "Could not find service",
            )
        if args[1] == "bootstrap":
            if self.fail_bootstrap:
                return subprocess.CompletedProcess(args, 5, "", "synthetic failure")
            self.loaded = True
        elif args[1] == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr("gtm.scheduler.platform.system", lambda: "Darwin")
    monkeypatch.setattr("gtm.scheduler.os.getuid", lambda: 501)
    project = tmp_path / "Project With Spaces"
    project.mkdir()
    (project / "pyproject.toml").touch()
    fake = FakeLaunchctl()
    agent = MacScheduler(
        project, project / "data/state/logs", home=tmp_path / "fake-home", runner=fake
    )
    return agent, fake


def test_plist_contract_and_native_macos_validation(tmp_path):
    spec = build_plist(Path.cwd(), tmp_path / "logs")
    content = validate_plist(spec)
    restored = plistlib.loads(content)
    assert restored["RunAtLoad"] is True and restored["StartInterval"] == 900
    assert restored["ProgramArguments"][0] == str(Path(sys.executable).absolute())
    assert restored["ProgramArguments"][1:] == ["-m", "gtm.cli", "import", "grok", "--all"]
    assert "ATTIO_API_KEY" not in content.decode()
    assert "PATH" not in restored["EnvironmentVariables"]
    path = tmp_path / "preview.plist"
    path.write_bytes(content)
    if Path("/usr/bin/plutil").is_file():
        # Read-only native validation; never launchctl bootstrap against the actual user domain.
        result = subprocess.run(
            ["/usr/bin/plutil", "-lint", str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_install_status_idempotency_uninstall_in_fake_home(scheduler):
    agent, fake = scheduler
    assert not agent.status()["automatic_ingestion_enabled"]
    installed = agent.install()
    assert installed["automatic_ingestion_enabled"] and agent.path.exists()
    assert agent.path.parts[-3:-1] == ("Library", "LaunchAgents")
    assert agent.path.stat().st_mode & 0o777 == 0o600
    assert (agent.logs / "grok-import.stdout.log").stat().st_mode & 0o777 == 0o600
    assert agent.status()["last_exit_code"] == "0"
    agent.install()
    assert sum(args[1] == "bootstrap" for args in fake.calls) == 1
    assert not agent.uninstall()["automatic_ingestion_enabled"]
    assert not agent.path.exists()
    assert agent.logs.exists()
    agent.uninstall()
    assert sum(args[1] == "bootout" for args in fake.calls) == 1


def test_bootstrap_failure_rolls_back_new_plist(scheduler):
    agent, fake = scheduler
    fake.fail_bootstrap = True
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        agent.install()
    assert not agent.path.exists()


def test_disabled_agent_and_foreign_paths_are_not_silently_replaced(scheduler):
    agent, fake = scheduler
    fake.disabled = True
    with pytest.raises(ValueError, match="disabled"):
        agent.install()
    fake.disabled = False
    agent.path.parent.mkdir(parents=True)
    other = {**agent.spec, "WorkingDirectory": "/somewhere/else"}
    agent.path.write_bytes(plistlib.dumps(other))
    with pytest.raises(ValueError, match="different paths"):
        agent.install()
    assert plistlib.loads(agent.path.read_bytes()) == other


def test_no_root_install(scheduler, monkeypatch):
    agent, _ = scheduler
    monkeypatch.setattr("gtm.scheduler.os.getuid", lambda: 0)
    with pytest.raises(ValueError, match="without sudo"):
        agent.install()
    assert not agent.path.exists()


def test_preview_cli_never_calls_launchctl_or_creates_user_agent(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("preview attempted a scheduler mutation")

    monkeypatch.setattr("gtm.scheduler.MacScheduler.install", forbidden)
    monkeypatch.setenv("ATTIO_API_KEY", "synthetic-secret-must-not-appear")
    preview = tmp_path / "preview.plist"
    result = CliRunner().invoke(
        app, ["scheduler", "install", "--dry-run", "--output-path", str(preview)]
    )
    assert result.exit_code == 0, result.output
    assert "synthetic-secret-must-not-appear" not in result.output + preview.read_text()
    assert plistlib.loads(preview.read_bytes())["StartInterval"] == 900


def test_plist_rejects_secret_environment(tmp_path):
    spec = build_plist(tmp_path, tmp_path / "logs")
    spec["EnvironmentVariables"]["ATTIO_API_KEY"] = "do-not-print"
    with pytest.raises(ValueError, match="credentials"):
        validate_plist(spec)


def test_scheduler_preview_cannot_write_into_launchagents(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import gtm.cli as cli

    install_folder = tmp_path / "Library/LaunchAgents"
    agent = SimpleNamespace(
        spec=build_plist(tmp_path, tmp_path / "logs"), path=install_folder / f"{LABEL}.plist"
    )
    monkeypatch.setattr(cli, "configured_scheduler", lambda: agent)
    result = CliRunner().invoke(
        app, ["scheduler", "install", "--dry-run", "--output-path", str(agent.path)]
    )
    assert result.exit_code == 1
    assert "outside" in result.output
    assert not install_folder.exists()
