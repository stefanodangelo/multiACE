#!/usr/bin/env bash
# Push the working tree to the printer without cutting a release.
#
# The existing update button installs PUBLISHED releases. This installs the
# code you have right now, which is what you actually need while developing.
#
# TWO PUSH CLASSES, and --web-only is the DEFAULT (plan §13.1):
#
#   --web-only (default)  syncs multiace/web/ only, restarts the panel
#                         service, never touches mcu.py, extruder.py or the
#                         Klipper extras, never restarts Klipper. This is
#                         the inner loop and it is mechanically inert - the
#                         worst case is a broken web page.
#
#   --full                syncs everything and runs install_multiace.sh,
#                         which REPLACES stock Klipper code (filament_feed,
#                         filament_switch_sensor, extruder) and sed-patches
#                         TRSYNC_TIMEOUT in mcu.py. TRSYNC_TIMEOUT governs
#                         multi-MCU homing, and homing failures are how a
#                         toolhead drives into the bed. So --full is gated
#                         on: local tests passing, the printer being idle,
#                         and the target's mcu.py hashing to either stock or
#                         the expected post-patch value.
#
#   --rollback            restores the installer's own backups
#                         (extruder_pre_multiace.py, mcu.py.pre_multiace)
#                         and restarts. Written and tested alongside --full,
#                         because a recovery path that has never run is not
#                         a recovery path.
#
#   --bootstrap-key       installs $MULTIACE_SSH_PUBKEY (default
#                         ~/.ssh/id_ed25519.pub) into the printer's
#                         authorized_keys, then exits - nothing else here
#                         runs. Needed after a reimage, when authorized_keys
#                         is gone and every other call in this script (ssh
#                         -o BatchMode=yes) refuses to fall back to a
#                         password prompt by design. Deliberately the one
#                         call that omits BatchMode, so ssh itself prompts
#                         for the printer's password at the console - it
#                         never passes through a shell variable, so it can't
#                         end up in shell history, an error message, or a
#                         process listing.
#
# Behaviour is shared with push-to-printer.ps1 - keep the two in step.
set -euo pipefail

HOST="${MULTIACE_PRINTER_HOST:-}"
USER_NAME="${MULTIACE_PRINTER_USER:-root}"
PUBKEY_PATH="${MULTIACE_SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"
MODE="web-only"
DRY_RUN=0
NO_RESTART=0
SKIP_TESTS=0
FORCE_MID_PRINT=0
ROLLBACK=0
BOOTSTRAP_KEY=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_TAR="/tmp/multiace-dev.tar.gz"
REMOTE_DIR="/tmp/multiace-dev"

# Value install_multiace.sh patches mcu.py to. Kept here so a third,
# unexpected hash can be refused rather than re-patched.
TRSYNC_VALUE="0.350"

usage() {
    cat <<'EOF'
Usage: push-to-printer.sh [options]

  --host <ip|name>   printer address (or $MULTIACE_PRINTER_HOST)
  --user <name>      ssh user (default root, or $MULTIACE_PRINTER_USER)
  --web-only         sync multiace/web only, no Klipper restart (DEFAULT)
  --full             sync everything and run install_multiace.sh
  --rollback         restore the installer's pre-multiACE backups, restart
  --bootstrap-key    install a pubkey into the printer's authorized_keys,
                     then exit (prompts for the printer's password)
  --pubkey <path>    public key to install with --bootstrap-key
                     (default ~/.ssh/id_ed25519.pub, or $MULTIACE_SSH_PUBKEY)
  --dry-run          print what would happen, change nothing
  --no-restart       skip every restart (implies no post-restart check)
  --skip-tests       skip the local pytest run (--full only; prints what it
                     is bypassing)
  --force-mid-print  proceed even while a print is running (never do this)
  -h, --help         this text
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --user) USER_NAME="$2"; shift 2 ;;
        --pubkey) PUBKEY_PATH="$2"; shift 2 ;;
        --web-only) MODE="web-only"; shift ;;
        --full) MODE="full"; shift ;;
        --rollback) ROLLBACK=1; shift ;;
        --bootstrap-key) BOOTSTRAP_KEY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --no-restart) NO_RESTART=1; shift ;;
        --skip-tests) SKIP_TESTS=1; shift ;;
        --force-mid-print) FORCE_MID_PRINT=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

if [ -z "$HOST" ]; then
    echo "error: no printer host. Pass --host or set MULTIACE_PRINTER_HOST." >&2
    exit 2
fi

say()  { printf 'STATUS: %s\n' "$*"; }
warn() { printf 'WARN:   %s\n' "$*" >&2; }
die()  { printf 'ERROR:  %s\n' "$*" >&2; exit 1; }

run_remote() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: ssh ${USER_NAME}@${HOST} $*"
        return 0
    fi
    ssh -o BatchMode=yes "${USER_NAME}@${HOST}" "$@"
}

# --- printer state (§13.4) -------------------------------------------------
# Same rule as the HTTP endpoints, and it fails CLOSED: an unreachable or
# unrecognised state blocks. "Cannot tell" and "idle" are not the same
# answer when the cost of being wrong is a ruined print.
check_printer_idle() {
    local state
    state="$(curl -fsS --max-time 5 \
        "http://${HOST}/printer/objects/query?print_stats" 2>/dev/null \
        | sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p' \
        | head -n1 || true)"
    if [ -z "$state" ]; then
        state="unknown"
    fi
    case "$state" in
        printing|paused|unknown)
            if [ "$FORCE_MID_PRINT" = "1" ]; then
                warn "printer reports '${state}' - proceeding anyway (--force-mid-print)"
                return 0
            fi
            die "printer is '${state}'. Installing now would abort the print, \
cut the heaters and leave the nozzle parked in setting plastic. \
Wait for it to finish, or pass --force-mid-print if you are certain."
            ;;
    esac
    say "printer state: ${state}"
}

# --- local tests (§13.1) ---------------------------------------------------
run_local_tests() {
    if [ "$SKIP_TESTS" = "1" ]; then
        warn "--skip-tests: NOT running pytest. You are about to install"
        warn "  multiace/klipper/extras/*.py, multiace/klipper/kinematics/extruder_ace.py"
        warn "  and let the installer sed-patch TRSYNC_TIMEOUT in mcu.py,"
        warn "  with no local check that any of it works."
        return 0
    fi
    say "running the local test suite (a tree that fails tests is never pushed)"
    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: python -m pytest -q"
        return 0
    fi
    ( cd "$REPO_ROOT" && python -m pytest -q ) \
        || die "tests failed - not pushing. Fix them, or --skip-tests if you \
truly mean to."
}

# --- mcu.py sha guard (§13.1) ---------------------------------------------
# The installer patches TRSYNC_TIMEOUT. A file that is neither stock nor
# already-patched has been hand-edited, and re-patching it is how a
# half-applied multi-MCU homing timeout gets shipped.
check_mcu_state() {
    local out
    out="$(run_remote "grep -E '^TRSYNC_TIMEOUT = ' /home/lava/klipper/klippy/mcu.py || true")" \
        || die "cannot read mcu.py on the printer"
    if [ "$DRY_RUN" = "1" ]; then return 0; fi
    local value
    value="$(printf '%s' "$out" | awk '{print $3}' | head -n1)"
    if [ -z "$value" ]; then
        die "no TRSYNC_TIMEOUT found in mcu.py - this is not a Klipper tree \
the installer knows how to patch."
    fi
    case "$value" in
        0.050|"$TRSYNC_VALUE") say "mcu.py TRSYNC_TIMEOUT=${value} (known)" ;;
        *) die "mcu.py has TRSYNC_TIMEOUT=${value}, which is neither stock \
(0.050) nor multiACE's (${TRSYNC_VALUE}). It has been hand-edited. Refusing \
to patch it again - multi-MCU homing depends on this value and a wrong one \
drives the toolhead into the bed. Restore mcu.py.pre_multiace first." ;;
    esac
}

# --- version stamp ---------------------------------------------------------
dev_version() {
    local sha dirty
    sha="$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo nogit)"
    dirty=""
    if ! ( cd "$REPO_ROOT" && git diff --quiet 2>/dev/null ); then
        dirty="-dirty"
    fi
    printf 'dev.%s%s' "$sha" "$dirty"
}

stamp_version() {
    local tag="$1" ace="$REPO_ROOT/multiace/klipper/extras/ace.py"
    [ -f "$ace" ] || return 0
    if grep -qE '^MULTIACE_BUILD_TAG' "$ace"; then
        say "build tag already present, leaving it"
        return 0
    fi
    say "stamping MULTIACE_BUILD_TAG = ${tag}"
    # Written into the tarball copy only - never into the working tree, so
    # a push never dirties your git status.
    printf '\nMULTIACE_BUILD_TAG = "%s"\n' "$tag" \
        >> "${STAGE_DIR}/multiace/klipper/extras/ace.py"
}

# --- rollback (§13.1) ------------------------------------------------------
do_rollback() {
    say "rolling back to the installer's pre-multiACE backups"
    run_remote 'set -e
K=/home/lava/klipper/klippy
did=0
if [ -f "$K/kinematics/extruder_pre_multiace.py" ]; then
    cp "$K/kinematics/extruder_pre_multiace.py" "$K/kinematics/extruder.py"
    echo "STATUS: restored extruder.py"
    did=1
fi
if [ -f "$K/mcu.py.pre_multiace" ]; then
    cp "$K/mcu.py.pre_multiace" "$K/mcu.py"
    echo "STATUS: restored mcu.py"
    did=1
fi
if [ "$did" = "0" ]; then
    echo "ERROR: no pre_multiace backups found - nothing to roll back" >&2
    exit 1
fi'
    [ "$NO_RESTART" = "1" ] || restart_klipper
    say "rollback done"
}

restart_klipper() {
    say "restarting Klipper"
    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: /etc/init.d/S60klipper restart"
        return 0
    fi
    # NOT firmware_restart, on purpose. Klipper's FIRMWARE_RESTART reloads
    # config and reinitialises the object graph WITHOUT re-`import`ing
    # already-loaded extras modules - Python's own sys.modules cache means
    # a --full push's changed .py files sit on disk, verified, completely
    # unused by the running process, and every symptom looks exactly like
    # the fix didn't work (observed HW-side 2026-08-13: five separate
    # source edits across a debugging session, each one silently ignored
    # by a Klipper process that had been running since before any of
    # them). Moonraker's own service-restart API is blocked for 'klipper'
    # on this appliance ("Service 'klipper' not allowed"), so the only way
    # to get a REAL process restart - a fresh `import`, not a reload - is
    # the init script itself, over the same SSH channel everything else
    # here already uses.
    run_remote '/etc/init.d/S60klipper restart' \
        || warn "S60klipper restart failed - check the printer"
}

restart_web() {
    say "restarting the multiACE web service"
    run_remote '/etc/init.d/S98multiace-web restart >/dev/null 2>&1 || true'
}

# Deliberately the one function that does NOT pass -o BatchMode=yes: its
# whole job is bootstrapping the key every other call here depends on. The
# key goes over stdin (never interpolated into the remote command string)
# so an unusual key comment can't break quoting. Dedup with grep -qxF so
# re-running this after it already succeeded is a no-op, not a growing
# authorized_keys file.
do_bootstrap_key() {
    [ -f "$PUBKEY_PATH" ] \
        || die "no public key at '${PUBKEY_PATH}' - pass --pubkey or set MULTIACE_SSH_PUBKEY."
    say "installing ${PUBKEY_PATH} on ${USER_NAME}@${HOST}"
    warn "this will prompt for the printer's password at the console (typed straight into ssh - never captured by this script)"
    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: <install ${PUBKEY_PATH} into ~/.ssh/authorized_keys>"
        return 0
    fi
    ssh "${USER_NAME}@${HOST}" '
set -e
umask 077
mkdir -p ~/.ssh
touch ~/.ssh/authorized_keys
key=$(cat)
grep -qxF "$key" ~/.ssh/authorized_keys || echo "$key" >> ~/.ssh/authorized_keys
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
' < "$PUBKEY_PATH" \
        || die "key bootstrap failed - check the password and that root login is permitted."
    say "public key installed - future pushes should authenticate without a password"
}

# --- post-restart verification (§13.1) ------------------------------------
# Verify recovery, do not assume it. A push that half-lands is exactly the
# "half-stock, half-multiACE" state the README warns about.
verify_recovery() {
    [ "$DRY_RUN" = "1" ] && return 0
    say "waiting for the printer to come back"
    local i health
    for i in $(seq 1 60); do
        health="$(curl -fsS --max-time 3 \
            "http://${HOST}/multiace/api/health" 2>/dev/null || true)"
        case "$health" in *'"status":"ok"'*|*'"status": "ok"'*)
            say "multiACE web is up"
            break ;;
        esac
        sleep 2
        if [ "$i" = "60" ]; then
            warn "multiACE web did not answer within 2 minutes"
        fi
    done
    local state
    state="$(curl -fsS --max-time 5 "http://${HOST}/multiace/api/state" \
        2>/dev/null || true)"
    case "$state" in
        *'"ace_startup"'*'"waiting"'*)
            warn "Klipper is up but an ACE did not reconnect. Power it on - \
multiACE picks it up within a few seconds now, or run ACE_RESCAN. \
If this push broke something, run: $0 --host ${HOST} --rollback" ;;
        "")
            warn "could not read /multiace/api/state - the push may have \
half-landed. If the printer misbehaves, run: $0 --host ${HOST} --rollback" ;;
        *)  say "Klipper reports ready and the ACEs reconnected" ;;
    esac
}

# --- main ------------------------------------------------------------------
if [ "$BOOTSTRAP_KEY" = "1" ]; then
    do_bootstrap_key
    exit 0
fi

if [ "$ROLLBACK" = "1" ]; then
    check_printer_idle
    do_rollback
    verify_recovery
    exit 0
fi

say "target: ${USER_NAME}@${HOST}  class: ${MODE}"
if [ "$MODE" = "full" ]; then
    say "FULL push: this replaces stock Klipper files and may patch mcu.py."
else
    say "WEB-ONLY push: multiace/web only. Klipper is not touched or restarted."
fi

STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

if [ "$MODE" = "full" ]; then
    run_local_tests
    check_printer_idle
    check_mcu_state
else
    # A broken panel mid-print is bad UX, not damage - warn, do not refuse.
    state="$(curl -fsS --max-time 5 \
        "http://${HOST}/printer/objects/query?print_stats" 2>/dev/null \
        | sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p' \
        | head -n1 || true)"
    case "$state" in
        printing|paused) warn "printer is ${state} - the panel will blink, \
but nothing mechanical is touched" ;;
    esac
fi

say "staging the working tree"
mkdir -p "${STAGE_DIR}/multiace"
if [ "$MODE" = "full" ]; then
    tar -C "$REPO_ROOT" \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
        -cf - multiace | tar -C "$STAGE_DIR" -xf -
else
    tar -C "$REPO_ROOT" \
        --exclude='__pycache__' --exclude='*.pyc' \
        -cf - multiace/web multiace/tools multiace/i18n \
              multiace/firmware_compat.py multiace/config_changes.py \
        | tar -C "$STAGE_DIR" -xf -
fi

# CRLF: this repo has no .gitattributes and Windows checkouts default to
# CRLF. busybox ash chokes on a `#!/bin/bash\r` shebang with a bare
# "cannot execute: required file not found" - no mention of line endings
# anywhere in that message. install_multiace.sh hit exactly this on a
# --full push: the file never got far enough to run its OWN internal
# `sed -i 's/\r$//'` self-repair (that fixes a copied init script, not
# itself) because the interpreter lookup for install_multiace.sh's own
# shebang fails before the script body executes at all. Same fix as
# build-paxx-mod.sh's strip_crlf: normalise on the way out, unconditionally
# (a no-op on an already-LF file), rather than trust the checkout.
find "${STAGE_DIR}/multiace" -type f \( -name '*.sh' -o -name '*.cfg' \
    -o -name '*.py' -o -name '*.json' -o -name 'S[0-9][0-9]*' \
    -o -name '*.sudoers' -o -name 'post-commit' \) \
    -exec sed -i 's/\r$//' {} +

stamp_version "$(dev_version)"

say "packing"
TARBALL="${STAGE_DIR}/multiace-dev.tar.gz"
tar -C "$STAGE_DIR" -czf "$TARBALL" multiace

if [ "$DRY_RUN" = "1" ]; then
    say "dry run: would copy $(du -h "$TARBALL" | awk '{print $1}') to ${HOST}"
    exit 0
fi

say "copying to ${HOST}:${REMOTE_TAR}"
# NOT scp, on purpose. Both scp protocol modes (the default SFTP-based
# transfer and the legacy -O one) took this printer's sshd down entirely -
# the first during protocol negotiation, the second mid-transfer - rather
# than just failing that one session (observed HW-side 2026-08-13). This
# streams the file over a plain SSH exec channel instead: the remote end
# only has to run `cat`, which needs nothing beyond the most basic SSH
# capability there is, not the scp/sftp subsystem that keeps crashing.
cat "$TARBALL" | ssh -T -o BatchMode=yes "${USER_NAME}@${HOST}" \
        "cat > '${REMOTE_TAR}'" \
    || die "streaming copy to the printer failed"

say "extracting"
run_remote "rm -rf '${REMOTE_DIR}' && mkdir -p '${REMOTE_DIR}' \
    && tar -C '${REMOTE_DIR}' -xzf '${REMOTE_TAR}'"

if [ "$MODE" = "full" ]; then
    say "running install_multiace.sh --install-web"
    run_remote "cd '${REMOTE_DIR}/multiace' && chmod +x install_multiace.sh \
        && ./install_multiace.sh --install-web"
    if [ "$NO_RESTART" = "0" ]; then
        restart_klipper
        verify_recovery
    fi
else
    # Mirror what install_multiace.sh does for these three directories -
    # same destinations, same flattening (multiace/i18n -> multiace_web/i18n,
    # which is where the backend resolves it as __file__/../../i18n), same
    # __pycache__ drop and chown. Inventing a layout here is how a push
    # lands somewhere the service does not read from.
    say "syncing the web files in place (no installer, no Klipper restart)"
    run_remote "set -e
WEB_DEST=/home/lava/multiace_web
if [ ! -d \"\$WEB_DEST/backend\" ]; then
    echo 'ERROR: '\"\$WEB_DEST\"' not found - multiACE Web has never been installed on this printer. Run a --full push first (it passes --install-web).' >&2
    exit 1
fi
mkdir -p \"\$WEB_DEST/backend\" \"\$WEB_DEST/frontend\" \"\$WEB_DEST/i18n\"
cp -a '${REMOTE_DIR}/multiace/web/backend/.'  \"\$WEB_DEST/backend/\"
cp -a '${REMOTE_DIR}/multiace/web/frontend/.' \"\$WEB_DEST/frontend/\"
cp -a '${REMOTE_DIR}/multiace/i18n/.'         \"\$WEB_DEST/i18n/\"
# main.py imports these two as siblings of itself (next to
# preflight_core.py) - they live at the top of the multiace/ tree in the
# repo, not under web/, so the tar above ships them separately and they
# need their own cp into backend/.
cp -f '${REMOTE_DIR}/multiace/firmware_compat.py' \"\$WEB_DEST/backend/\"
cp -f '${REMOTE_DIR}/multiace/config_changes.py'  \"\$WEB_DEST/backend/\"
# Stale bytecode may be root-owned from an older install; never fatal,
# because Python recompiles whenever the .py is newer anyway.
rm -rf \"\$WEB_DEST/backend/__pycache__\" 2>/dev/null || true
chown -R lava:lava \"\$WEB_DEST\" 2>/dev/null || true
mkdir -p /home/lava/printer_data/config/tools
cp -f '${REMOTE_DIR}/multiace/tools/'*.py /home/lava/printer_data/config/tools/
echo 'STATUS: synced backend, frontend, i18n and tools'"
    if [ "$NO_RESTART" = "0" ]; then
        restart_web
    fi
fi

say "done"
