#!/usr/bin/env bash
# 手动总开关：本机/NAS/任意 cron 都能跑，不依赖任何 Actions 调度。
#   GH_ACTIONS_PAT=ghp_xxx ./kick.sh              # 补齐今天缺的包（第二波等上游，上限 30 分钟）
#   GH_ACTIONS_PAT=ghp_xxx ./kick.sh --dry-run     # 只看计划
#   GH_ACTIONS_PAT=ghp_xxx ./kick.sh --only zonghe,dayitong --downstream-wait 900
set -euo pipefail
cd "$(dirname "$0")"
: "${GH_ACTIONS_PAT:=? 需要 PAT（fine-grained：Contents: RW + Actions: RW，勾上那 17 个仓）}"
export GH_ACTIONS_PAT
exec python3 -u scripts/sweep.py "$@"
