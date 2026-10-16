#!/usr/bin/env bash
set -euo pipefail
export DEPLOY_ENV=preprod
exec bash "$(dirname "$0")/actionengine_deploy.sh"
