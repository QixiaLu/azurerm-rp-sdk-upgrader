"""upgrader — a minimal Ralph loop that upgrades one Azure RP until ``go build`` passes.

Each iteration is a fresh Copilot session that reads IMPLEMENTATION_PLAN.md + the code,
does one bounded chunk, runs ``go build``, updates the plan, and writes result.json. The
loop repeats until the sidecar reports the build is green or it hits the iteration cap.
Disk is the only shared state between iterations.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
