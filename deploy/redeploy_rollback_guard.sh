#!/usr/bin/env bash
# Small rollback transaction used by redeploy.sh once production topology changes begin.

ROLLBACK_GUARD_ARMED=0
ROLLBACK_GUARD_RESTORE_FN=""
ROLLBACK_GUARD_CLEANUP_FN=""

rollback_guard_on_exit() {
    local exit_rc=$?
    local callback_rc=0
    trap - EXIT INT TERM

    if [ "$ROLLBACK_GUARD_ARMED" = "1" ]; then
        "$ROLLBACK_GUARD_RESTORE_FN" || callback_rc=1
    fi
    "$ROLLBACK_GUARD_CLEANUP_FN" || callback_rc=1
    if [ "$callback_rc" -ne 0 ]; then
        exit_rc=1
    fi
    exit "$exit_rc"
}

rollback_guard_arm() {
    ROLLBACK_GUARD_RESTORE_FN="$1"
    ROLLBACK_GUARD_CLEANUP_FN="$2"
    ROLLBACK_GUARD_ARMED=1
    trap rollback_guard_on_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

rollback_guard_disarm() {
    ROLLBACK_GUARD_ARMED=0
}
