#!/bin/bash
set -e
cd /home/dean/workspace/phd/htc-platform
python3 tools/interscsimulator_scenario/convert_interscsimulator.py \
  --network tools/interscsimulator_scenario/input/base_scenario/mini-map.xml \
  --trips tools/interscsimulator_scenario/input/base_scenario/mini-trips.xml \
  --projection matsim-sp-utm23s \
  --start-unit minutes \
  --format json \
  --synth-bike-fraction 0.0 \
  --synth-bus-lines 1 \
  --synth-bus-stops-per-line 2 \
  --synth-buses-per-line 1 \
  --synth-bus-headway 600 \
  --synth-subway-lines 0 \
  --no-synth-transit \
  --output /home/dean/workspace/phd/htc-platform/tools/interscsimulator_scenario/output/mini_test \
  --scenario-name mini_test
echo "DONE"
