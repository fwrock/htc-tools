#!/usr/bin/env python3
"""Generate an HTC scenario from the native São Paulo MATSim export."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scenario_common.matsim import main

if __name__ == "__main__":
    main(city="sao_paulo")
