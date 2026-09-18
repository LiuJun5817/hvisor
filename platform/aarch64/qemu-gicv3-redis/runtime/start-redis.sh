#!/bin/bash
set -euo pipefail

EVAL_DIR=${EVAL_DIR:-/eval}
"$EVAL_DIR/bin/prepare-redis.sh"
"$EVAL_DIR/bin/start-zone.sh" primary
"$EVAL_DIR/bin/wait-primary.sh"
"$EVAL_DIR/bin/start-zone.sh" replica
"$EVAL_DIR/bin/wait-deployment.sh"
