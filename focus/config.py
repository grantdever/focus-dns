"""Paths and constants. Everything is overridable so tests never touch the real files or network."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 4861
# Placeholder inside 100.64.0.0/10 so tests run anywhere. The real address comes from FOCUS_TAILNET_IP
# (environment or the env file); bin/install-unit fills it in from `tailscale ip -4`.
TAILNET_IP = "100.64.0.1"
QUERY_CAP = 300_000


def _home() -> Path:
    return Path.home()


@dataclass
class Config:
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("FOCUS_STATE_DIR", _home() / ".local/state/focus")))
    env_file: Path = field(default_factory=lambda: Path(os.environ.get("FOCUS_ENV", _home() / ".config/focus/nextdns.env")))
    token_file: Path = field(default_factory=lambda: Path(os.environ.get("FOCUS_TOKEN", _home() / ".config/focus/token")))
    unit_dir: Path = field(default_factory=lambda: _home() / ".config/systemd/user")
    port: int = DEFAULT_PORT
    tailnet_ip: str = ""
    # False in tests: no systemctl / systemd-run calls.
    systemd: bool = True

    def __post_init__(self) -> None:
        self.tailnet_ip = self.tailnet_ip or self.setting("FOCUS_TAILNET_IP") or TAILNET_IP

    def setting(self, key: str, default: str = "") -> str:
        """A FOCUS_* setting: the environment first, then the env file."""
        if os.environ.get(key):
            return os.environ[key]
        try:
            return self.read_env().get(key, default)
        except OSError:
            return default

    @property
    def exclude_devices(self) -> set[str] | None:
        """NextDNS device names that are machines, not people (FOCUS_EXCLUDE_DEVICES). None: the module default."""
        raw = self.setting("FOCUS_EXCLUDE_DEVICES")
        return {d.strip().lower() for d in raw.split(",") if d.strip()} or None

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def reports_dir(self) -> Path:
        return self.state_dir / "reports"

    @property
    def flip_dropin_dir(self) -> Path:
        return self.unit_dir / "focus-flip.timer.d"

    @property
    def flip_dropin(self) -> Path:
        return self.flip_dropin_dir / "schedule.conf"

    def read_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self.env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            out[k] = v.strip().strip('"').strip("'")
        return out

    def read_token(self) -> str | None:
        try:
            t = self.token_file.read_text().strip()
        except OSError:
            return None
        return t if len(t) >= 20 else None
