#!/usr/bin/env bash
# Run the validator test suite.
#
# PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 is REQUIRED: the validator venv ships
# anchorpy, whose pytest11 entry point imports pytest_asyncio (not
# installed), and autoloading it aborts the whole run at collection time.
#
# No GPU job, no real keypair, no PM2 process is touched by this suite.
set -euo pipefail
cd "$(dirname "$0")"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
exec ./.venv-test/bin/python -m pytest "$@"
