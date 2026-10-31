#!/usr/bin/env bash
set -euo pipefail

# Immutable Release-Lock construction was retired with the direct-Compose
# deployment model. Historical release files remain audit evidence; this
# command must not create a new lock-era release directory.
printf '%s\n' \
  'RETIRED_RELEASE_LOCK_BUILD: configure a direct deployment with bin/configure-p8' >&2
exit 64
