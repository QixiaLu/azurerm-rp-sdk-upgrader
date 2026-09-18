"""Process-local credential store: secrets stay out of ``os.environ``.

``.env`` mixes values every child process legitimately needs (``GH_TOKEN`` for the Copilot
SDK, ``GOPROXY``, proxy settings) with credentials only two subprocesses need — the Azure
service principal and the TeamCity token. Exporting the whole file means every command an
agent runs inherits the service principal, and one stray ``printenv`` puts it in the
transcript. Those keys are held here instead and injected explicitly via ``env=`` at the
single spawn site that uses them.

This removes *incidental* exposure; it is not a containment boundary. An agent with shell
access can still read the ``.env`` file.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["ENV_FILE_VAR", "SECRET_KEYS", "get", "load", "parse", "subprocess_env"]

# Kept in-process. Everything else in `.env` is exported so tools that only read the
# environment (Copilot SDK, go, pip, corporate proxies) keep working unchanged.
SECRET_KEYS = frozenset({
    "ARM_CLIENT_CERTIFICATE_PASSWORD",
    "ARM_CLIENT_ID",
    "ARM_CLIENT_SECRET",
    "ARM_SUBSCRIPTION_ID",
    "ARM_TENANT_ID",
    "TEAMCITY_ACCESSTOKEN",
    "TEAMCITY_TOKEN",
})

# Names the loaded file so agent-spawned ``python -m upgrader.helpers`` subprocesses resolve
# the same credentials the orchestrator did. A path, not a secret.
ENV_FILE_VAR = "UPGRADER_ENV_FILE"

_held: dict[str, str] = {}


def parse(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` file into a dict; ``{}`` when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if not separator or not key:
            continue
        pairs[key] = value.strip().strip('"').strip("'")
    return pairs


def _candidates(explicit: Path | None) -> list[Path]:
    if explicit is not None:
        return [explicit]
    pointer = os.environ.get(ENV_FILE_VAR)
    if pointer:
        return [Path(pointer)]
    package_root = Path(__file__).resolve().parent.parent
    return list(dict.fromkeys([Path.cwd() / ".env", package_root / ".env"]))


def _harvest_environment() -> None:
    """Move secrets the process already inherited into the store.

    ``docker compose``'s ``env_file`` and a shell ``export`` both put them in ``os.environ``
    before Python starts, so the store has to claim them rather than merely avoid adding them.
    """
    for key in SECRET_KEYS:
        value = os.environ.pop(key, None)
        if value:
            _held.setdefault(key, value)


def load(explicit: Path | None = None) -> Path | None:
    """Load ``explicit`` if given, else ``./.env`` and this checkout's root ``.env``.

    Secret keys are held in-process, the rest exported. Values already present in the real
    environment always win. Returns the first file that yielded anything, or ``None``.
    """
    _harvest_environment()
    loaded: Path | None = None
    for path in _candidates(explicit):
        pairs = parse(path)
        if not pairs:
            continue
        for key, value in pairs.items():
            if key in SECRET_KEYS:
                _held.setdefault(key, value)
            elif key not in os.environ:
                os.environ[key] = value
        loaded = loaded or path
    if loaded is not None:
        os.environ[ENV_FILE_VAR] = str(loaded)
    return loaded


def get(name: str, default: str | None = None) -> str | None:
    """Resolve a value: the real environment first, then the held secrets."""
    return os.environ.get(name) or _held.get(name) or default


def subprocess_env(*names: str) -> dict[str, str]:
    """``os.environ`` plus the named held secrets — for one ``env=`` spawn."""
    env = dict(os.environ)
    for name in names:
        value = _held.get(name)
        if value:
            env[name] = value
    return env
