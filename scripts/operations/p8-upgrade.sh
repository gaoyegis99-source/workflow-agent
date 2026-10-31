#!/usr/bin/env bash
set -euo pipefail

# The Release-Lock upgrade state machine is historical recovery evidence, not
# an active deployment mechanism. Direct Compose maintenance is deliberately
# explicit: enable maintenance mode, make and verify a cold backup, configure
# the new instance, then use bin/p8 preflight/up/status.
printf '%s\n' \
  'RETIRED_RELEASE_LOCK_UPGRADE: use bin/configure-p8 and bin/p8 direct-Compose maintenance' >&2
exit 64
