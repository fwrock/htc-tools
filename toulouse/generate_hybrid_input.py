#!/usr/bin/env python3
"""Toulouse compatibility entry point for the shared MATSim -> HTC converter."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scenario_common import matsim

if __name__ == "__main__":
    matsim.main(city="toulouse")
else:
    # Preserve the public Python API, including patching parser classes in callers.
    sys.modules[__name__] = matsim
