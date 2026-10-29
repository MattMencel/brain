"""`brain install` and its steps. Each step is idempotent and also its own subcommand, so a failed one re-runs alone."""

import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from brain import config, connectors, db, secrets

LABEL = "local.brain"
SKILL_LINK = Path.home() / ".claude" / "skills" / "brain"


def write_config(args=None) -> int:
    if config.PATH.exists():
        print(f"config: {config.PATH} exists")
        return 0
    config.PATH.parent.mkdir(parents=True, exist_ok=True)
    text = files("brain").joinpath("brain.example.toml").read_text()
    name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, check=False).stdout.strip()
    if name:  # seeds "me"; add the names other sources show for you (Jira, Slack display names)
        text += f'\n[people.me]\naliases = [{json.dumps(name)}]\n'
    config.PATH.write_text(text)
    print(f"config: wrote {config.PATH}; edit it to choose connectors and repo roots")
    return 0


def db_up(args=None) -> int:
    if db.mode() == "docker":
        compose = str(files("brain").joinpath("docker-compose.yml"))
        subprocess.run(["docker", "compose", "-f", compose, "-p", "brain", "up", "-d", "--wait"], check=True)
    db.connect().close()
    print(f"db: {db.dsn()} reachable")
    return 0


def migrate(args=None) -> int:
    db.migrate(db.connect())
    print("db: schema up to date")
    return 0


def auth(args) -> int:
    secrets.prompt(args.connector)
    print(f"auth: stored {args.connector} token in the OS keyring")
    return 0


def auth_missing(args=None) -> int:
    """Prompt for each enabled connector that needs a secret and has none."""
    mods = connectors.registry()
    missing = [n for n in config.enabled(list(mods)) if getattr(mods[n], "SECRET", False) and not secrets.get(n)]
    for name in missing:
        if not sys.stdin.isatty():
            print(f"auth: {name} has no token; run brain auth {name}", file=sys.stderr)
            return 1
        secrets.prompt(name)
    print("auth: every enabled connector has its secret")
    return 0


def schedule_install(args=None) -> int:
    sched = config.load().get("schedule", {})
    minutes, limit = sched.get("interval_minutes", 30), sched.get("limit", 40)
    exe = shutil.which("brain") or str(Path(sys.argv[0]).resolve())
    cmd = f'"{exe}" ingest --limit {limit}; "{exe}" check'
    system = platform.system()
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        log = str(Path.home() / "Library" / "Logs" / "brain.log")
        plist.write_bytes(plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": ["/bin/sh", "-c", cmd],
            "EnvironmentVariables": {"PATH": os.environ["PATH"]},  # claude and git as this shell finds them
            "StartInterval": minutes * 60,
            "RunAtLoad": True,
            "LowPriorityIO": True,
            "Nice": 10,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }))
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True, check=False)  # absent on first install
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
        print(f"schedule: {plist}, every {minutes} min, log {log}")
        return 0
    if system == "Linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "brain.service").write_text(
            "[Unit]\nDescription=brain ingest\n\n[Service]\nType=oneshot\n"
            f"Environment=PATH={os.environ['PATH']}\nExecStart=/bin/sh -c '{cmd}'\nNice=10\n"
        )
        (unit_dir / "brain.timer").write_text(
            f"[Unit]\nDescription=brain ingest every {minutes} min\n\n"
            f"[Timer]\nOnBootSec=2min\nOnUnitActiveSec={minutes}min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "brain.timer"], check=True)
        print(f"schedule: systemd user timer brain.timer, every {minutes} min; logs: journalctl --user -u brain")
        return 0
    print(f"schedule: {system} is not supported; run brain ingest yourself", file=sys.stderr)
    return 1


def link_skill(args=None) -> int:
    src = Path(str(files("brain").joinpath("skill")))
    if SKILL_LINK.exists() or SKILL_LINK.is_symlink():
        same = SKILL_LINK.resolve() == src.resolve()
        print(f"skill: {SKILL_LINK} exists" + ("" if same else f", not pointing at {src}; left alone"))
        return 0
    SKILL_LINK.parent.mkdir(parents=True, exist_ok=True)
    SKILL_LINK.symlink_to(src)
    print(f"skill: linked {SKILL_LINK} -> {src}")
    return 0


def install(args) -> int:
    for step in (write_config, db_up, migrate, auth_missing, schedule_install, link_skill):
        if rc := step():
            return rc
    return 0
