#!/usr/bin/env bash
# Toulouse: full scenario, 1 % of the population, with bus + rail/subway/tram.
# MESO by default; set MICRO_RATIO (0-1) for a hybrid MESO/MICRO network, e.g. MICRO_RATIO=0.2.
#
# Public transport is NOT downloaded: toulouse_transit_schedule.xml.gz already carries the buses,
# regional trains (TER), metro and tram of the region (derived upstream from the national GTFS/NeTEx
# feeds), and the converter reads that MATSim schedule, not GTFS. Everything it needs is in input/.
#
# Usage:  ./generate_tl_1pct_meso.sh
# Override with environment variables, e.g.:
#   SCENARIO=tl_1pct_v2 OUTPUT=/data/htc/input UNSUPPORTED_MODE=walk ./generate_tl_1pct_meso.sh
#   DOCKER_BASE=/scenarios ./generate_tl_1pct_meso.sh      # paths valid inside docker compose
#   SCENARIO=tl_1pct_micro MICRO_RATIO=0.2 MICRO_STRATEGY=highway ./generate_tl_1pct_meso.sh
#
# pt and car_passenger trips have no native HTC leg, so a policy is mandatory:
#   replan (default) -> runtime PendingDecision among MC_MODES; needs --transit/--rail
#   walk             -> WalkLeg (counted, changes those persons' travel pattern)
#   skip-person      -> drop the person
set -euo pipefail
cd "$(dirname "$0")"

SCENARIO="${SCENARIO:-tl_1pct_meso}"
OUTPUT="${OUTPUT:-output}"
INPUT_DIR="${INPUT_DIR:-input}"
SAMPLE="${SAMPLE:-0.01}"
SEED="${SEED:-42}"
# Parent directory of the scenario folders AS THE ENGINE SEES IT; baked into simulation.json paths.
# Docker Compose mounts the scenarios at /scenarios (see docs/DOCKER_COMPOSE.md).
DOCKER_BASE="${DOCKER_BASE:-/app/hyperbolic-time-chamber/simulations/input}"
MICRO_RATIO="${MICRO_RATIO:-0}"              # 0 = every link MESO
MICRO_STRATEGY="${MICRO_STRATEGY:-highway}"  # highway | arterial | random
UNSUPPORTED_MODE="${UNSUPPORTED_MODE:-replan}"
MC_MODES="${MC_MODES:-bus subway walk}"
MAX_BUSES_PER_ROUTE="${MAX_BUSES_PER_ROUTE:-10}"
MAX_TRAINS_PER_LINE="${MAX_TRAINS_PER_LINE:-6}"
OUTPUT_FORMAT="${OUTPUT_FORMAT:-json}"
# Optional local OSM XML/XML.gz extract for traffic lights (path relative to INPUT_DIR).
SIGNAL_OSM_FILE="${SIGNAL_OSM_FILE:-}"
SIGNAL_ARGS=()
if [[ -n "$SIGNAL_OSM_FILE" ]]; then
  SIGNAL_ARGS=(--signals --signal-osm-file "$SIGNAL_OSM_FILE"
               --signal-cycle "${SIGNAL_CYCLE:-120}"
               --signal-max-snap-distance "${SIGNAL_MAX_SNAP_DISTANCE:-50}")
fi

for f in toulouse_network.xml.gz toulouse_population.xml.gz \
         toulouse_transit_schedule.xml.gz toulouse_transit_vehicles.xml.gz; do
  [[ -e "$INPUT_DIR/$f" ]] || { echo "ERROR: missing $INPUT_DIR/$f" >&2; exit 1; }
done
if [[ -n "$(ls -A "$OUTPUT/$SCENARIO" 2>/dev/null)" ]]; then
  echo "ERROR: $OUTPUT/$SCENARIO already exists and is not empty." >&2
  echo "       Use a new SCENARIO name or remove it first (the generator refuses to mix runs)." >&2
  exit 1
fi

# --micro-ratio 0 => every link is MESO. --max-transit-routes 0 => all bus routes, not the first 500.
# shellcheck disable=SC2086  # MC_MODES is intentionally word-split into separate arguments
exec python3 generate_hybrid_input.py \
  --scenario "$SCENARIO" \
  --input-dir "$INPUT_DIR" \
  --output "$OUTPUT" \
  --output-format "$OUTPUT_FORMAT" \
  --docker-base "$DOCKER_BASE" \
  --sample "$SAMPLE" \
  --seed "$SEED" \
  --micro-ratio "$MICRO_RATIO" --micro-strategy "$MICRO_STRATEGY" \
  --transit --max-transit-routes 0 --max-buses-per-route "$MAX_BUSES_PER_ROUTE" \
  --rail --max-trains-per-line "$MAX_TRAINS_PER_LINE" \
  "${SIGNAL_ARGS[@]}" \
  --unsupported-mode "$UNSUPPORTED_MODE" \
  --mc-modes $MC_MODES
