"""upgrader — a minimal driver that upgrades one Azure RP until ``go build`` passes.

A fresh Copilot session reads IMPLEMENTATION_PLAN.md + the code, does one bounded chunk of
the upgrade, runs ``go build``, updates the plan, and writes result.json. Success is reported
only when the sidecar records a green build. Disk is the only shared state.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
