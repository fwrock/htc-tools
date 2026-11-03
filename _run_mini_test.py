#!/usr/bin/env python3
"""Run mini test scenario generation."""
import sys
sys.path.insert(0, "/home/dean/workspace/phd/htc-platform/tools/interscsimulator_scenario")
from convert_interscsimulator import main
main([
    "--network", "/home/dean/workspace/phd/htc-platform/tools/interscsimulator_scenario/input/base_scenario/mini-map.xml",
    "--trips", "/home/dean/workspace/phd/htc-platform/tools/interscsimulator_scenario/input/base_scenario/mini-trips.xml",
    "--projection", "matsim-sp-utm23s",
    "--start-unit", "minutes",
    "--format", "json",
    "--synth-bike-fraction", "0.0",
    "--synth-bus-lines", "0",
    "--synth-subway-lines", "0",
    "--output", "/home/dean/workspace/phd/htc-platform/tools/interscsimulator_scenario/output/mini_test",
    "--scenario-name", "mini_test",
])
