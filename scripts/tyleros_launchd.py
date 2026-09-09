#!/usr/bin/env python3
"""Prepare (never install) a macOS LaunchAgent, or run its deterministic worker."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import sys
from urllib.parse import urlsplit

LABEL = "com.tyleros.mobile-companion"
ALLOWED = {"TYLEROS_URL", "TYLEROS_RUNTIME_CREDENTIAL", "TYLEROS_POLL_SECONDS", "TYLEROS_VERCEL_PROTECTION_BYPASS"}


def read_config(path: Path) -> dict[str, str]:
    """No shell sourcing, symlink following, permissive file modes, or secret output."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Config must be a private regular file owned by this user (mode 600).")
        if info.st_size > 16384:
            raise ValueError("Config is too large.")
        config = json.load(source)
    if not isinstance(config, dict) or not set(config) <= ALLOWED:
        raise ValueError("Config has unsupported keys.")
    if not all(isinstance(value, str) for value in config.values()):
        raise ValueError("Config values must be strings.")
    url = urlsplit(config.get("TYLEROS_URL", ""))
    if (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})) or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        raise ValueError("Worker URL must be an HTTPS origin or local loopback origin.")
    credential = config.get("TYLEROS_RUNTIME_CREDENTIAL", "")
    if not re.fullmatch(r"tylrt_[A-Za-z0-9_-]{43}", credential) or len(set(credential[6:])) < 8:
        raise ValueError("A generated instance credential is required.")
    seconds = config.get("TYLEROS_POLL_SECONDS", "30")
    if not seconds.isdigit() or not 15 <= int(seconds) <= 300:
        raise ValueError("Poll interval must be 15 to 300 seconds.")
    config["TYLEROS_POLL_SECONDS"] = seconds
    bypass = config.get("TYLEROS_VERCEL_PROTECTION_BYPASS")
    if bypass is not None and (not bypass or any(ord(c) < 33 or ord(c) > 126 for c in bypass)):
        raise ValueError("Protection bypass must be a nonempty header-safe secret.")
    return config


def agent_plist(config_path: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(Path(sys.executable).resolve()), str(Path(__file__).resolve()), "run", str(config_path.absolute())],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "ProcessType": "Background",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "run"])
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        if args.mode == "prepare":
            if args.output is None:
                raise ValueError("An output path is required.")
            # Exclusive creation prevents accidental replacement of another service.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as target:
                plistlib.dump(agent_plist(args.config), target)
            print("Prepared LaunchAgent. No service was installed or started.")
            return 0
        worker = Path(__file__).resolve().parent.parent / "tyleros_worker.py"
        # Do not inherit provider keys, system tokens, or other assistant capabilities.
        env = {"PATH": os.defpath, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", **config}
        os.execve(sys.executable, [sys.executable, str(worker)], env)
    except (OSError, ValueError):
        print("Worker setup failed. Check the private config permissions, allowed settings, and destination path. No secret details were logged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
