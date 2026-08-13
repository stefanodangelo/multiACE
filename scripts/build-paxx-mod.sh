#!/usr/bin/env bash
# Build the PAXX mod overlay that turns this working tree into a firmware .bin.
#
# NOTHING HERE PRODUCES A .bin. This script produces the OVERLAY that the
# PAXX build system bakes into one. The chain is:
#
#   this repo  ->  40-feature-multiace/ overlay  ->  PAXX build  ->  U1_*.bin
#                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^      ^^^^^^^^^^
#                  what this script emits           not ours; needs Docker
#
# The last hop is PAXX's (https://github.com/paxx12-snapmaker-u1/
# SnapmakerU1-Extended-Firmware). It repackages a signed Snapmaker image,
# which is why its output opens from the printer's own update screen and a
# hand-rolled blob does not. Pass --into <paxx-fork> to drop the overlay
# straight into a checkout and print the exact build command.
#
# LAYOUT is not ours to invent - it is PAXX's overlay contract, and every
# destination below was read off decay71's shipped v0.99.6.2b_mod release.
# A file in the wrong place is silently not installed, so deviations from
# that reference are called out in comments where they exist.
#
# Behaviour is shared with push-to-printer.sh in spirit: fail closed, say
# what it is doing, never touch the working tree.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GLUE_DIR="${REPO_ROOT}/scripts/paxx-overlay"
MOD_NAME="40-feature-multiace"

# aarch64 is the U1's SoC (Rockchip). The image has no pip at build time, so
# the web backend's deps are vendored in as prebuilt wheels; 3.11 matches the
# firmware's interpreter and the site-packages path baked in below.
WHEEL_PLATFORM="manylinux2014_aarch64"
PY_VERSION="3.11"
SITE_PACKAGES="home/lava/.local/lib/python3.11/site-packages"

OUT_DIR="${REPO_ROOT}/dist"
PAXX_FORK=""
PAXX_REPO_URL="https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware.git"
PROFILE="extended"
SKIP_WHEELS=0
DRY_RUN=0
BUILD_BIN=0

usage() {
    cat <<'EOF'
Usage: build-paxx-mod.sh [options]

  --bin              go all the way to a flashable .bin: install the
                     overlay into a PAXX checkout and run PAXX's Docker
                     build. Needs Docker Desktop running. Implies --into
                     (auto-clones one next to this repo if --into is not
                     also given).
  --into <dir>       PAXX fork to install the overlay into, at
                     <dir>/overlays/firmware-extended/40-feature-multiace/.
                     With --bin and a dir that does not exist yet, it is
                     cloned from --paxx-repo first.
  --paxx-repo <url>  where to clone the PAXX fork from, when --bin needs
                     one and --into points at a directory that does not
                     exist yet (default: paxx12-snapmaker-u1's upstream)
  --profile <name>   PAXX PROFILE to build (default: extended)
  --out <dir>        where to write the tarball and, with --bin, the .bin
                     (default: ./dist)
  --skip-wheels      do not download the aarch64 wheels. The image will
                     have no FastAPI/uvicorn, so the Web UI cannot start.
                     Only useful for checking the layout quickly.
  --dry-run          print what would happen, write nothing
  -h, --help         this text

Without --bin, requires: python3 with pip (wheel download), tar.
With --bin, also requires: git, Docker Desktop running, several GB free,
and time - it downloads stock Snapmaker firmware and builds in a container.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --bin) BUILD_BIN=1; shift ;;
        --into) PAXX_FORK="$2"; shift 2 ;;
        --paxx-repo) PAXX_REPO_URL="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        --skip-wheels) SKIP_WHEELS=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

if [ "$BUILD_BIN" = "1" ] && [ -z "$PAXX_FORK" ]; then
    PAXX_FORK="$(cd "$REPO_ROOT/.." && pwd)/SnapmakerU1-Extended-Firmware"
fi

say()  { printf 'STATUS: %s\n' "$*"; }
warn() { printf 'WARN:   %s\n' "$*" >&2; }
die()  { printf 'ERROR:  %s\n' "$*" >&2; exit 1; }

[ -d "$GLUE_DIR" ] || die "missing ${GLUE_DIR} - the overlay glue (init \
scripts, the firmware-config tweak hook, install_web.sh) lives there and \
cannot be derived from the source tree."

# Fail before the expensive staging/wheel work, not after: --bin has real
# prerequisites (git, a running Docker daemon) that a missing overlay file
# does not, and finding out 5 minutes in wastes the wheel download too.
if [ "$BUILD_BIN" = "1" ] && [ "$DRY_RUN" = "0" ]; then
    command -v git >/dev/null 2>&1 || die "--bin needs git (to fetch the \
PAXX fork) and it is not on PATH."
    # A starting-up or wedged daemon can leave `docker version` hanging on
    # the pipe instead of erroring, so this stops waiting rather than
    # blocking the whole build indefinitely on a check meant to fail fast.
    DOCKER_PROBE="docker version"
    if command -v timeout >/dev/null 2>&1; then DOCKER_PROBE="timeout 15 docker version"; fi
    if ! $DOCKER_PROBE >/dev/null 2>&1; then
        die "--bin needs Docker, and the daemon is not reachable (or did \
not answer within 15s). Start Docker Desktop, wait for it to finish \
starting, and try again. (This is PAXX's build requirement, not this \
script's - dev.sh sets up its Debian Trixie ARM64 container on top of it.)"
    fi
fi

VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/multiace/VERSION")"
[ -n "$VERSION" ] || die "multiace/VERSION is empty"

# --- version derivation ----------------------------------------------------
# multiace/VERSION is the marketing version and it does not move between
# releases, so it cannot be the whole artifact name: two builds of different
# commits would both be multiace-0.99.6.2b-paxx-mod.tar.gz and the second
# would silently overwrite the first. Git already knows the answer - the repo
# carries release tags (v0.99.6.2b etc.) - so ask it.
#
#   HEAD is exactly a tag, tree clean  -> release:  0.99.6.2b
#   anything else                      -> dev:      0.99.6.2b.dev7.g138463a[.dirty]
#
# The dev form is unique per commit, so builds accumulate in dist/ instead of
# clobbering each other, and a flashed image can always be traced back.
SHA="$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo nogit)"
DIRTY=""
if ! ( cd "$REPO_ROOT" && git diff --quiet 2>/dev/null ) \
   || ! ( cd "$REPO_ROOT" && git diff --cached --quiet 2>/dev/null ); then
    DIRTY=".dirty"
fi

EXACT_TAG="$(cd "$REPO_ROOT" && git describe --exact-match --tags HEAD 2>/dev/null || true)"
IS_RELEASE=0
if [ -n "$EXACT_TAG" ] && [ -z "$DIRTY" ]; then
    IS_RELEASE=1
    ARTIFACT_VERSION="$VERSION"
    # A tag that disagrees with VERSION means one of the two was not bumped.
    # Do not guess which - name both and let the user decide.
    TAG_VERSION="${EXACT_TAG#v}"
    if [ "$TAG_VERSION" != "$VERSION" ]; then
        warn "tag ${EXACT_TAG} and multiace/VERSION (${VERSION}) disagree. \
Naming the artifact after VERSION. Bump one of them if this is a release."
    fi
else
    COMMITS_SINCE="$(cd "$REPO_ROOT" \
        && git rev-list --count "$(git describe --tags --abbrev=0 2>/dev/null || echo HEAD)"..HEAD 2>/dev/null \
        || echo 0)"
    ARTIFACT_VERSION="${VERSION}.dev${COMMITS_SINCE}.g${SHA}${DIRTY}"
fi
# Keep the name filesystem- and PAXX-safe regardless of what git produced.
ARTIFACT_VERSION="$(printf '%s' "$ARTIFACT_VERSION" | tr -c 'A-Za-z0-9._-' '-')"

# --- CRLF ------------------------------------------------------------------
# This repo has no .gitattributes and the working tree is CRLF. busybox ash
# does not tolerate a `#!/bin/sh\r` shebang - the init scripts would fail at
# boot with a bare "not found", and install_multiace.sh already carries a
# `sed -i 's/\r$//'` for exactly this reason. Strip CR from everything
# text-shaped on the way in rather than trusting the checkout.
strip_crlf() {
    find "$1" -type f \( -name '*.sh' -o -name '*.cfg' -o -name '*.py' \
        -o -name '*.json' -o -name '*.yaml' -o -name '*.conf' \
        -o -name 'S[0-9][0-9]*' -o -name '*.sudoers' -o -name 'post-commit' \) \
        -exec sed -i 's/\r$//' {} +
}

# install -D, but portable and quiet about it.
put() {
    local src="$1" dst="$2"
    [ -f "$src" ] || die "expected file missing from the working tree: $src"
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
}

say "multiACE ${ARTIFACT_VERSION} -> ${MOD_NAME} overlay"
if [ "$DRY_RUN" = "1" ]; then
    say "dry run: no files will be written"
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
MOD="${STAGE}/${MOD_NAME}"
ROOT="${MOD}/root"

# ---------------------------------------------------------------------------
# 1. the glue that is not derivable from the source tree
# ---------------------------------------------------------------------------
# S54-S58 (tweak symlink, ace.cfg refresh, TRSYNC mask, tools sync, mode
# swap), the firmware-config settings hook that puts multiACE in the PAXX UI,
# and install_web.sh. The SSH installer does these jobs imperatively; a
# flashed image has no installer run, so they become boot hooks.
say "staging overlay glue from scripts/paxx-overlay/"
mkdir -p "$MOD"
cp -R "${GLUE_DIR}/root" "$MOD/"
cp -R "${GLUE_DIR}/test" "$MOD/"

# ---------------------------------------------------------------------------
# 2. the Klipper engine
# ---------------------------------------------------------------------------
say "staging Klipper extras and kinematics"
for f in "${REPO_ROOT}"/multiace/klipper/extras/*.py; do
    put "$f" "${ROOT}/home/lava/klipper/klippy/extras/$(basename "$f")"
done
put "${REPO_ROOT}/multiace/klipper/kinematics/extruder_ace.py" \
    "${ROOT}/home/lava/klipper/klippy/kinematics/extruder_ace.py"

# ---------------------------------------------------------------------------
# 3. boot hooks that DO live in the source tree
# ---------------------------------------------------------------------------
put "${REPO_ROOT}/multiace/deploy/S59multiace-prewarm" \
    "${ROOT}/etc/init.d/S59multiace-prewarm"
put "${REPO_ROOT}/multiace/web/deploy/S98multiace-web" \
    "${ROOT}/etc/init.d/S98multiace-web"
put "${REPO_ROOT}/multiace/web/deploy/multiace-web.nginx.conf" \
    "${ROOT}/etc/nginx/fluidd.d/multiace-web.conf"
put "${REPO_ROOT}/multiace/web/deploy/multiace-debug.sudoers" \
    "${ROOT}/etc/sudoers.d/multiace-debug"

# ---------------------------------------------------------------------------
# 4. the multiACE source bundle at /home/lava/multiace
# ---------------------------------------------------------------------------
say "staging the source bundle (web, i18n, tools)"
mkdir -p "${ROOT}/home/lava/multiace"
tar -C "${REPO_ROOT}/multiace" \
    --exclude='__pycache__' --exclude='*.pyc' \
    -cf - web i18n | tar -C "${ROOT}/home/lava/multiace" -xf -
# _import_sibling's checkout-relative fallback candidate expects these
# beside multiace/web/, matching the real checkout layout - keep this
# bundle a genuine mirror rather than a web-only subset.
cp "${REPO_ROOT}/multiace/firmware_compat.py" "${ROOT}/home/lava/multiace/"
cp "${REPO_ROOT}/multiace/config_changes.py"  "${ROOT}/home/lava/multiace/"

# tools/: ship what the printer and the backend actually import. NOTE this
# deliberately ships MORE than the reference release, which predates
# job_history.py / swap_cost.py - the backend imports both, and a missing
# module there is a 500 on the dashboard, not a degraded feature.
# Excluded: git-hooks/ and generate_testmatrix.py (developer-only, inert in
# an image) and the stray *.log in the tree.
mkdir -p "${ROOT}/home/lava/multiace/tools"
for f in "${REPO_ROOT}"/multiace/tools/*.py "${REPO_ROOT}"/multiace/tools/*.sh; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in
        generate_testmatrix.py) continue ;;
    esac
    put "$f" "${ROOT}/home/lava/multiace/tools/$(basename "$f")"
done
put "${REPO_ROOT}/multiace/tools/multiace_v2d.init" \
    "${ROOT}/home/lava/multiace/tools/multiace_v2d.init"

# The updater is read from /home/lava by the Config-tab update button.
put "${REPO_ROOT}/multiace/tools/multiace_update.sh" \
    "${ROOT}/home/lava/multiace_update.sh"

# S55multiace-cfg-refresh calls this by an absolute path, under a DIFFERENT
# name than it has in the repo. Renaming it here is not cosmetic: get it
# wrong and the boot hook silently falls back to clobbering the user's
# ace.cfg with the shipped default instead of merging it.
put "${REPO_ROOT}/multiace/tools/merge_ace_cfg.py" \
    "${ROOT}/usr/local/bin/multiace_merge_cfg.py"

# ---------------------------------------------------------------------------
# 5. the pre-installed web service at /home/lava/multiace_web
# ---------------------------------------------------------------------------
# Same flattening install_multiace.sh and push-to-printer.sh use: i18n lands
# beside backend/, which is where main.py resolves it as __file__/../../i18n.
say "staging the installed web service layout"
mkdir -p "${ROOT}/home/lava/multiace_web"
tar -C "${REPO_ROOT}/multiace/web" \
    --exclude='__pycache__' --exclude='*.pyc' \
    -cf - backend frontend | tar -C "${ROOT}/home/lava/multiace_web" -xf -
rm -rf "${ROOT}/home/lava/multiace_web/backend/__pycache__"
mkdir -p "${ROOT}/home/lava/multiace_web/i18n"
cp "${REPO_ROOT}"/multiace/i18n/*.json "${ROOT}/home/lava/multiace_web/i18n/"
# main.py imports these as siblings of itself (next to preflight_core.py,
# already covered by the backend/ tar above) - they live at the top of
# multiace/, not under web/, and every other deploy path (install_multiace.sh,
# both push scripts) needs the identical extra copy for the same reason.
# Without it the backend crashes on import at startup: FileNotFoundError on
# firmware_compat.py, no traceback anywhere near obviously related to a
# build-script omission.
cp "${REPO_ROOT}/multiace/firmware_compat.py" "${ROOT}/home/lava/multiace_web/backend/"
cp "${REPO_ROOT}/multiace/config_changes.py"  "${ROOT}/home/lava/multiace_web/backend/"
# deploy/ is build input, not runtime state - it belongs to the source bundle.
rm -rf "${ROOT}/home/lava/multiace_web/deploy"

# ---------------------------------------------------------------------------
# 6. firmware-config: what PAXX mirrors into printer_data
# ---------------------------------------------------------------------------
# PAXX's S49extended-config mirrors firmware-config/extended/ into
# printer_data/config/extended/ on boot. That mirror is why ace_vars.cfg and
# materials.json are NOT shipped here even though the repo has them: they
# are user state, and a shipped copy would be re-applied over the user's
# tuning on every single boot. They are created at first run instead.
say "staging firmware-config (tweak hook + mirrored config)"
FWC="${ROOT}/usr/local/share/firmware-config"
put "${REPO_ROOT}/multiace/config/extended/ace.cfg" "${FWC}/extended/ace.cfg"
put "${REPO_ROOT}/multiace/config/extended/multiace/ace_mode_switch.sh" \
    "${FWC}/extended/multiace/ace_mode_switch.sh"
mkdir -p "${FWC}/extended/multiace/i18n"
cp "${REPO_ROOT}"/multiace/i18n/*.json "${FWC}/extended/multiace/i18n/"

# ---------------------------------------------------------------------------
# 7. vendored aarch64 wheels
# ---------------------------------------------------------------------------
# The firmware has no pip at build time, so the backend's dependency tree is
# unpacked into --user site-packages ahead of time. --only-binary is
# mandatory: an sdist would try to compile aarch64 C extensions (uvloop,
# pydantic-core) on this machine, which cannot work.
if [ "$SKIP_WHEELS" = "1" ]; then
    warn "--skip-wheels: no FastAPI/uvicorn in the image. The Web UI will \
not start. Layout check only."
else
    say "downloading ${WHEEL_PLATFORM} / py${PY_VERSION} wheels"
    # Probe for an interpreter that RUNS, not one that merely exists on
    # PATH. On this Windows box `python3` is a stale 3.7 shim that dies with
    # a missing api-ms-win-crt DLL, while `python` is a healthy 3.11 - a
    # `command -v python3` check passes and then every later call fails.
    PY=""
    for cand in python3 python py; do
        command -v "$cand" >/dev/null 2>&1 || continue
        if "$cand" -c 'import sys, pip' >/dev/null 2>&1; then PY="$cand"; break; fi
    done
    [ -n "$PY" ] || die "no working python with pip found (tried python3, \
python, py). Needed to download the wheels; use --skip-wheels only if you \
do not need the Web UI."
    say "using interpreter: ${PY} ($("$PY" -V 2>&1))"
    WHEEL_TMP="${STAGE}/wheels"
    mkdir -p "$WHEEL_TMP" "${ROOT}/${SITE_PACKAGES}"
    # --platform picks wheel TAGS for the target, but environment markers
    # (`sys_platform == "win32"`) are still evaluated against THIS machine.
    # Building on Windows therefore resolves uvicorn[standard] wrongly: it
    # pulls colorama (win32-only) and drops uvloop (non-win32 only), so the
    # image silently loses uvicorn's fast event loop. Ask for the non-win32
    # members explicitly and drop the host-only ones after the fact.
    "$PY" -m pip download \
        --quiet \
        --requirement "${REPO_ROOT}/multiace/web/backend/requirements.txt" \
        --dest "$WHEEL_TMP" \
        --only-binary=:all: \
        --platform "$WHEEL_PLATFORM" \
        --python-version "$PY_VERSION" \
        --retries 5 --timeout 30 \
        || die "wheel download failed. Either a dep has no aarch64 wheel for \
py${PY_VERSION}, or PyPI was unreachable - the message above says which. \
Either way this stops rather than half-populating the image."
    for extra in uvloop; do
        if ! ls "$WHEEL_TMP"/${extra}-*.whl >/dev/null 2>&1; then
            say "marker fixup: fetching ${extra} (excluded by a win32 marker)"
            "$PY" -m pip download --quiet "$extra" \
                --dest "$WHEEL_TMP" --no-deps --only-binary=:all: \
                --platform "$WHEEL_PLATFORM" --python-version "$PY_VERSION" \
                || die "could not fetch ${extra} for ${WHEEL_PLATFORM}"
        fi
    done
    for host_only in colorama pywin32 pywin32_ctypes; do
        rm -f "$WHEEL_TMP"/${host_only}-*.whl 2>/dev/null || true
    done
    # A wheel is a zip; unpacking it is what pip would do anyway, and this
    # keeps the .dist-info that makes the result introspectable on-device.
    for w in "$WHEEL_TMP"/*.whl; do
        [ -f "$w" ] || die "no wheels were downloaded"
        "$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
            "$w" "${ROOT}/${SITE_PACKAGES}"
    done
    say "vendored $(find "$WHEEL_TMP" -name '*.whl' | wc -l | tr -d ' ') wheels"
    # Fail closed on the two that decide whether the service starts at all.
    for must in fastapi uvicorn uvloop pydantic_core; do
        ls -d "${ROOT}/${SITE_PACKAGES}/${must}"* >/dev/null 2>&1 \
            || die "${must} missing from the vendored site-packages - the \
web service would not start on the printer."
    done
fi

# ---------------------------------------------------------------------------
# 8. normalise, stamp, pack
# ---------------------------------------------------------------------------
say "stripping CR"
strip_crlf "$MOD"

# --- version stamping ------------------------------------------------------
# Both constants the post-commit hook maintains have to be stamped here, and
# for the same reason: they are what ACE_HEAD_STATUS reports on the printer.
# Written to the staged copy only - the working tree is never touched.
TAG="${ARTIFACT_VERSION}"
ACE_PY="${ROOT}/home/lava/klipper/klippy/extras/ace.py"

# MULTIACE_BUILD_TAG. This REPLACES an existing value, where
# push-to-printer.sh leaves one alone: ace.py carries a checked-in tag, and
# shipping that stale value means a user reporting a bug against a flashed
# .bin names a commit that is not the one in their image.
if grep -qE '^MULTIACE_BUILD_TAG' "$ACE_PY"; then
    sed -i -E "s|^MULTIACE_BUILD_TAG = .*|MULTIACE_BUILD_TAG = \"${TAG}\"|" "$ACE_PY"
else
    printf '\nMULTIACE_BUILD_TAG = "%s"\n' "$TAG" >> "$ACE_PY"
fi
# -F, not -E: the version can contain '+', which an ERE reads as a quantifier.
grep -qF "MULTIACE_BUILD_TAG = \"${TAG}\"" "$ACE_PY" \
    || die "failed to stamp the build tag into ace.py"

# MULTIACE_BUNDLE_SHA1, recomputed over the STAGED bundle - not copied from
# the repo, and the distinction is the whole point.
#
# ace.py::_compute_bundle_sha1 rehashes these three files ON THE PRINTER, at
# their post-mode-switch stock names, and ACE_HEAD_STATUS prints PASS/FAIL
# against the baked-in constant. So the constant must describe the bytes this
# overlay actually ships. It does not describe the bytes in the repo: this
# checkout is CRLF and the overlay is CR-stripped, so hashing the working
# tree yields a different digest than hashing what lands in the image (94f44e6
# vs 95ac875 at the time of writing - the checked-in value happens to match
# the stripped form, so the repo is self-consistent today by luck, and the
# post-commit hook run on a CRLF checkout would break it). Hashing the staged
# copies is correct either way, and stays correct when a bundle file changes.
#
# Order must match BUNDLE_FILES in multiace/tools/git-hooks/post-commit.
BUNDLE_SHA="$(cat \
    "${ROOT}/home/lava/klipper/klippy/extras/filament_feed_ace.py" \
    "${ROOT}/home/lava/klipper/klippy/extras/filament_switch_sensor_ace.py" \
    "${ROOT}/home/lava/klipper/klippy/kinematics/extruder_ace.py" \
    | sha1sum | cut -c1-7)"
grep -qE '^MULTIACE_BUNDLE_SHA1' "$ACE_PY" \
    || die "no MULTIACE_BUNDLE_SHA1 in ace.py - the runtime bundle check \
this stamps has moved or been removed; reconcile with \
multiace/tools/git-hooks/post-commit before shipping an image."
REPO_BUNDLE_SHA="$(grep -oE '^MULTIACE_BUNDLE_SHA1 = "[^"]*"' "$ACE_PY" \
    | sed 's/.*"\(.*\)"/\1/')"
sed -i -E "s|^MULTIACE_BUNDLE_SHA1 = .*|MULTIACE_BUNDLE_SHA1 = \"${BUNDLE_SHA}\"|" "$ACE_PY"
grep -qF "MULTIACE_BUNDLE_SHA1 = \"${BUNDLE_SHA}\"" "$ACE_PY" \
    || die "failed to stamp the bundle sha1 into ace.py"

say "build tag:  ${TAG}"
say "bundle sha: ${BUNDLE_SHA}"
if [ "$REPO_BUNDLE_SHA" != "$BUNDLE_SHA" ]; then
    say "  (repo had ${REPO_BUNDLE_SHA}; recomputed from the staged bundle)"
fi
if [ "$IS_RELEASE" = "1" ]; then
    say "release build from tag ${EXACT_TAG}"
else
    warn "not a release build (untagged commit and/or dirty tree). The image \
will contain your uncommitted changes and reports itself as ${TAG}."
fi
sed -e "s|@VERSION@|${VERSION}|g" -e "s|@TAG@|${TAG}|g" \
    "${GLUE_DIR}/README.md" > "${MOD}/README.md"

if [ "$DRY_RUN" = "1" ]; then
    say "dry run: overlay assembled at ${MOD} ($(du -sh "$MOD" | awk '{print $1}'))"
    say "dry run: would write ${OUT_DIR}/multiace-${ARTIFACT_VERSION}-paxx-mod.tar.gz"
    find "$MOD" -path '*/site-packages' -prune -o -type f -print \
        | sed "s|${STAGE}/||" | sort
    if [ -n "$PAXX_FORK" ]; then
        if [ -d "$PAXX_FORK" ]; then
            say "dry run: would install overlay into ${PAXX_FORK}"
        else
            say "dry run: would clone ${PAXX_REPO_URL} -> ${PAXX_FORK}"
        fi
    fi
    if [ "$BUILD_BIN" = "1" ]; then
        say "dry run: would run in ${PAXX_FORK}:"
        say "  ./dev.sh make tools && ./dev.sh make firmware && \\"
        say "  ./dev.sh make build PROFILE=${PROFILE} OUTPUT_FILE=firmware/U1_${PROFILE}.bin"
        say "dry run: would write ${OUT_DIR}/multiace-${ARTIFACT_VERSION}-${PROFILE}.bin"
    fi
    exit 0
fi

mkdir -p "$OUT_DIR"
TARBALL="${OUT_DIR}/multiace-${ARTIFACT_VERSION}-paxx-mod.tar.gz"
say "packing ${TARBALL}"

# Packed by Python, not tar, to set modes and ownership EXPLICITLY.
#
# chmod is a silent no-op on the MSYS/Windows filesystem this may run on -
# the modes that reach the tarball would otherwise be whatever heuristic the
# host applied (git-bash guesses from content, so .py lands 0644 and .sh
# lands 0755 by luck rather than intent). An init script that arrives
# non-executable does not fail loudly; the printer just boots without
# multiACE. So the rules are declared here and applied at pack time on any
# host: root:root, 0755 for the boot hooks and the scripts users invoke,
# 0440 for sudoers (sudo refuses a group/world-writable drop-in outright),
# 0644 for everything else. mtime is pinned to the commit for a
# reproducible artifact.
PACK_PY="${STAGE}/pack.py"
cat > "$PACK_PY" <<'PYEOF'
import os, sys, tarfile

stage, mod_name, out, mtime = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])

EXEC_EXACT = {
    "root/home/lava/multiace/install_web.sh",
    "root/home/lava/multiace_update.sh",
    "root/usr/local/share/firmware-config/extended/multiace/ace_mode_switch.sh",
}
SUDOERS = "root/etc/sudoers.d/multiace-debug"

def mode_for(rel):
    # rel is POSIX, relative to the mod dir
    if rel.startswith("root/etc/init.d/"):
        return 0o755
    if rel == SUDOERS:
        return 0o440
    if rel in EXEC_EXACT:
        return 0o755
    return 0o644

def norm(ti):
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    ti.mtime = mtime
    rel = ti.name.split("/", 1)[1] if "/" in ti.name else ""
    if ti.isdir():
        ti.mode = 0o755
    elif ti.isfile():
        ti.mode = mode_for(rel)
    return ti

count = 0
with tarfile.open(out, "w:gz") as tf:
    for dirpath, dirnames, filenames in os.walk(os.path.join(stage, mod_name)):
        dirnames.sort(); filenames.sort()
        for name in [None] + filenames:
            full = dirpath if name is None else os.path.join(dirpath, name)
            arc = os.path.relpath(full, stage).replace(os.sep, "/")
            tf.add(full, arcname=arc, recursive=False, filter=norm)
            if name is not None:
                count += 1
print("packed %d files" % count)
PYEOF

PACK_INTERP=""
for cand in "${PY:-}" python3 python py; do
    [ -n "$cand" ] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import tarfile' >/dev/null 2>&1; then PACK_INTERP="$cand"; break; fi
done
COMMIT_TIME="$(cd "$REPO_ROOT" && git log -1 --format=%ct 2>/dev/null || echo 0)"
if [ -n "$PACK_INTERP" ]; then
    "$PACK_INTERP" "$PACK_PY" "$STAGE" "$MOD_NAME" "$TARBALL" "$COMMIT_TIME" \
        || die "packing failed"
else
    # Only reached with no usable python at all, which also means
    # --skip-wheels was in play. GNU tar can still set ownership, but the
    # per-file modes then come from the host filesystem.
    warn "no python available to pack - falling back to tar. File modes will \
be whatever this filesystem reports, which on Windows is a guess. Do not \
publish this artifact; rebuild it on Linux or WSL."
    tar -C "$STAGE" --owner=root --group=root -czf "$TARBALL" "$MOD_NAME"
fi
( cd "$OUT_DIR" && sha256sum "$(basename "$TARBALL")" \
    > "$(basename "$TARBALL").sha256" )
say "wrote $(du -h "$TARBALL" | awk '{print $1}') + .sha256"

# ---------------------------------------------------------------------------
# 9. optionally drop the overlay into a PAXX fork, and optionally build
# ---------------------------------------------------------------------------
if [ -n "$PAXX_FORK" ]; then
    if [ ! -d "$PAXX_FORK" ]; then
        if [ "$BUILD_BIN" = "1" ]; then
            say "cloning PAXX fork -> ${PAXX_FORK}"
            # -c core.autocrlf=false: PAXX's own repo has no .gitattributes,
            # so on a machine with Windows Git's common core.autocrlf=true
            # default, a plain clone rewrites every LF shell script it
            # ships (dev.sh, scripts/create_firmware.sh, all 60+ overlay
            # scripts) to CRLF on checkout. Those run inside the Linux
            # container this build spins up, and a `#!/bin/bash\r` shebang
            # fails there with "env: 'bash\r': No such file or directory" -
            # not a build bug, a checkout setting. Scoped to this one clone
            # via -c, not the user's global git config.
            git clone -c core.autocrlf=false "$PAXX_REPO_URL" "$PAXX_FORK" \
                || die "clone failed: ${PAXX_REPO_URL}"
        else
            die "no such directory: $PAXX_FORK"
        fi
    fi
    # Refuse a wrong target rather than scattering an overlay into an
    # unrelated tree and leaving the user to wonder why the bake ignored it.
    [ -f "${PAXX_FORK}/Makefile" ] && [ -d "${PAXX_FORK}/overlays" ] \
        || die "${PAXX_FORK} does not look like a PAXX firmware checkout \
(no Makefile + overlays/)."
    DEST="${PAXX_FORK}/overlays/firmware-extended/${MOD_NAME}"
    say "installing overlay -> ${DEST}"
    rm -rf "$DEST"
    mkdir -p "$(dirname "$DEST")"
    cp -R "$MOD" "$DEST"
    # The tarball gets its modes fixed at pack time; a directory copy cannot,
    # so on a filesystem where chmod does nothing the baked image can end up
    # with a non-executable init script. Probe rather than assume.
    PROBE="${STAGE}/.chmod-probe"
    : > "$PROBE"; chmod 0755 "$PROBE" 2>/dev/null || true
    CHMOD_WORKS=1
    if [ "$(stat -c '%a' "$PROBE" 2>/dev/null)" != "755" ]; then
        CHMOD_WORKS=0
        warn "this filesystem ignores chmod, so the overlay copied into the \
PAXX fork has host-guessed permissions."
    fi

    if [ "$BUILD_BIN" = "0" ]; then
        if [ "$CHMOD_WORKS" = "0" ]; then
            warn "verify /etc/init.d/S5* under ${DEST} are 0755 before \
building, or unpack ${TARBALL} there instead - its modes are correct."
        fi
        cat <<EOF

Overlay is in place. Build the image from ${PAXX_FORK}:

    ./dev.sh make tools
    ./dev.sh make firmware
    ./dev.sh make build PROFILE=${PROFILE} OUTPUT_FILE=firmware/U1_${PROFILE}.bin

dev.sh sets up the Debian Trixie ARM64 container, so Docker must be running.
The .bin lands at OUTPUT_FILE and is what you open from the printer's update
screen. Run './dev.sh make mods' to see the firmwares and mods available.
Or re-run this script with --bin to do all of that automatically.
EOF
    else
        # If the modes could not survive a directory copy, fix them straight
        # in the PAXX checkout so the build that is about to run gets the
        # same bytes the tarball would have - not a best-effort guess.
        if [ "$CHMOD_WORKS" = "0" ]; then
            say "re-extracting the (correctly-moded) tarball over ${DEST}"
            rm -rf "$DEST"
            tar -C "$(dirname "$DEST")" -xzf "$TARBALL"
        fi

        # PAXX depends on at least one git submodule (deps/screen-apps).
        # A plain `git clone` - what this script (and a manual `git clone`
        # of the fork) does - leaves submodule paths as empty directories;
        # nothing about --into or a fresh clone initialises them. That is
        # invisible until a much later overlay tries to build inside one
        # and hits a make error that looks nothing like "missing
        # submodule" - e.g. 61-app-remote-screen's install script running
        # `make install` in deps/screen-apps got "No rule to make target
        # 'install'" because the directory was empty, not because the
        # Makefile lacked that rule. Always sync, whether the fork was just
        # cloned or --into pointed at a pre-existing checkout that itself
        # never ran this.
        if [ -d "${PAXX_FORK}/.git" ]; then
            # Persist core.autocrlf=false in the LOCAL config, not just as a
            # transient `-c` on the earlier clone. `-c` during `git clone`
            # only affects that one checkout operation - it is never
            # written to .git/config, confirmed by inspecting it here
            # (no autocrlf line under [core], so this repo has been
            # inheriting the machine-global true the whole time regardless
            # of the clone flag). That matters beyond the initial checkout:
            # with autocrlf=true active, `git diff` normalises both sides
            # before comparing and can report NO difference between a
            # CRLF-committed blob and an LF working-tree file, which is
            # exactly the count the CRLF sweep below depends on to know
            # what it fixed. Worse, ANY future git write in this repo
            # during the build (checkout, stash, another submodule sync)
            # would silently re-apply CRLF, undoing the sed fix after the
            # fact. Set it for real, before doing anything else that
            # touches files.
            git -C "$PAXX_FORK" config core.autocrlf false

            say "syncing git submodules"
            ( cd "$PAXX_FORK" && git submodule update --init --recursive ) \
                || die "submodule sync failed - deps/screen-apps (and any \
other submodule) would be missing, and a later overlay's build against it \
would fail with an unrelated-looking error."

            # Each submodule is its own repo with its own local config,
            # seeded from the global default (true here) regardless of what
            # the superproject's config says - same landmine, one level
            # deeper.
            ( cd "$PAXX_FORK" && git submodule foreach --recursive \
                'git config core.autocrlf false' ) >/dev/null \
                || warn "could not set core.autocrlf=false in one or more \
submodules - their files may still be CRLF-corrupted by git operations \
during the build."
        fi

        # Belt and suspenders on the CRLF issue above: the -c core.autocrlf
        # clone flag only helps a fork THIS script just cloned. --into can
        # also point at a fork the user already checked out (with whatever
        # autocrlf setting was in effect then), so probe the fork's own
        # files regardless of how it got here and normalise if needed.
        #
        # NOT scoped to *.sh: vars.mk is CRLF too, and it is `source`d by
        # bash (not exec'd), so it does not fail loudly like a bad shebang -
        # it silently appends \r to every variable it sets. That took down
        # a build much further downstream: 10-patch-kernel-modules compares
        # the boot image's kernel string against $KERNEL_VERSION, which was
        # "6.1.99\r", so a CORRECT kernel was reported as a version mismatch
        # and the build aborted. An extension whitelist (*.sh, *.mk, ...)
        # would just repeat that mistake with the next file type PAXX
        # happens to source. Ask git which tracked files are text instead -
        # `git grep -Il ''` matches everything git's own binary-detection
        # would diff/patch as text, which is exactly the set that can break
        # this way.
        if [ -d "${PAXX_FORK}/.git" ]; then
            # --recurse-submodules: the submodule sync above is a fresh git
            # clone in its own right, made on the same machine with the
            # same core.autocrlf setting - deps/screen-apps's own scripts
            # can be just as CRLF-broken as the superproject's were.
            mapfile -t FORK_TEXT_FILES < <(cd "$PAXX_FORK" && git grep --recurse-submodules -Il '' -- . 2>/dev/null \
                | grep -v "^overlays/firmware-extended/${MOD_NAME}/")
            # Count via `file` BEFORE sed runs, not via `git diff` after.
            # Two independent tools turned out to be the wrong instrument
            # here: MSYS/git-bash's grep opens files in Windows text mode
            # and strips \r before the pattern ever sees it, so
            # `grep -l $'\r$' vars.mk` reports NO match even though
            # `od -c vars.mk` clearly shows \r\n. And `git diff --quiet`
            # after the fact is not reliable either - once autocrlf-driven
            # corruption is repaired, the file matches its original
            # committed blob again (confirmed directly: deps/screen-apps's
            # Makefile blob is plain LF, so a repaired working-tree copy is
            # BYTE-IDENTICAL to the index and `git diff` correctly, but
            # unhelpfully, reports no difference - that is success, not a
            # missed detection, but it means diffing after the fix cannot
            # tell you what the fix changed). `file`, which reads raw bytes
            # with no git or MSYS translation in the way, has been the one
            # tool all session that reported CRLF exactly when `od -c`
            # confirmed it - so count with it, before touching anything.
            FORK_CRLF_COUNT=0
            for f in "${FORK_TEXT_FILES[@]}"; do
                [ -n "$f" ] || continue
                case "$(file -b "${PAXX_FORK}/${f}" 2>/dev/null)" in
                    *CRLF*) FORK_CRLF_COUNT=$((FORK_CRLF_COUNT + 1)) ;;
                esac
            done
            # sed runs unconditionally regardless of the count above - it is
            # a no-op on an already-LF file, and the count is only for the
            # message below, never a gate on whether the fix applies.
            for f in "${FORK_TEXT_FILES[@]}"; do
                [ -n "$f" ] || continue
                sed -i 's/\r$//' "${PAXX_FORK}/${f}"
            done
            if [ "$FORK_CRLF_COUNT" -gt 0 ]; then
                warn "normalised ${FORK_CRLF_COUNT} of ${#FORK_TEXT_FILES[@]} \
tracked text files in ${PAXX_FORK} from CRLF to LF - this checkout was made \
with Git's core.autocrlf=true. (This checkout only; not touching git config \
or history.)"
            fi
        else
            warn "${PAXX_FORK} has no .git directory, so CRLF normalisation \
can only guess by file extension instead of asking git which files are \
text - some non-*.sh/*.mk file PAXX sources could still be CRLF and break \
in ways that do not look like a line-ending problem. Prefer --into pointing \
at a git checkout of PAXX."
            find "$PAXX_FORK" \( -name '*.sh' -o -name '*.mk' -o -name 'Makefile' \) \
                -not -path "*/${MOD_NAME}/*" \
                -exec sed -i 's/\r$//' {} +
        fi

        # tmp/firmware/ is create_firmware.sh's mutable extraction workspace
        # (rk-unpacked/, boot-unpacked/, rootfs/), rebuilt fresh every build.
        # It is NOT safe to reuse: an interrupted run (killed mid-unsquashfs,
        # a crashed container, Ctrl-C) can leave it half-extracted, and the
        # next run's unsquashfs then collides with its own leftover output -
        # e.g. "failed to create symlink .../bin, because File exists". That
        # is a stale-workspace error, not a real build failure, and it looks
        # nothing like one. Always start clean.
        if [ -d "${PAXX_FORK}/tmp/firmware" ]; then
            say "clearing stale build workspace ${PAXX_FORK}/tmp/firmware \
(leftover from a prior run - the download and tool caches in tmp/cache are \
untouched)"
            rm -rf "${PAXX_FORK}/tmp/firmware"
        fi

        # tmp/cache/ mostly holds honestly-idempotent downloads (curl,
        # rsync, cloned repos) and is worth keeping across runs - re-fetching
        # it every build would be slow for no benefit. But at least one
        # entry is not a pure cache: overlays/.../compile_livemedia.sh
        # extracts the live555 tarball and then `cp`s a config file INSIDE
        # that extracted tree - an in-place mutation, not a download. The
        # live555 tarball itself ships some files read-only (-r--r--r--),
        # and once that cp has run once, an interrupted rerun leaves a
        # read-only copy sitting where the next run's plain `cp` (no -f)
        # needs to overwrite it - "Permission denied", again a stale-state
        # error rather than a real build problem. There is no complete list
        # of which tmp/cache subtrees do this, so rather than guess which
        # ones are safe to keep as-is, make everything writable - cheap,
        # non-destructive (nothing is removed, so the expensive downloads
        # are still skipped), and it can only help.
        if [ -d "${PAXX_FORK}/tmp/cache" ]; then
            chmod -R u+w "${PAXX_FORK}/tmp/cache" 2>/dev/null || true
        fi

        BIN_NAME="U1_${PROFILE}.bin"
        BIN_OUTPUT_REL="firmware/${BIN_NAME}"
        say "building PROFILE=${PROFILE} in ${PAXX_FORK} (this downloads \
stock firmware and runs a Docker build - it can take a while the first time)"
        (
            cd "$PAXX_FORK"
            [ -x ./dev.sh ] || chmod +x ./dev.sh 2>/dev/null || true
            # git-bash/MSYS rewrites POSIX-looking args into Windows paths
            # before handing them to non-MSYS executables. dev.sh passes
            # `$(pwd)` to `docker run -w`, so on Windows that arrives as
            # `C:/Users/...` instead of a path the Linux daemon accepts,
            # and every docker invocation fails with "needs to be an
            # absolute path" no matter how the build itself is set up.
            # MSYS_NO_PATHCONV (and belt-and-suspenders MSYS2_ARG_CONV_EXCL)
            # turn that rewriting off; both are no-ops outside MSYS, so this
            # is harmless on WSL/Linux/macOS.
            export MSYS_NO_PATHCONV=1
            export MSYS2_ARG_CONV_EXCL='*'
            ./dev.sh make tools
            ./dev.sh make firmware
            ./dev.sh make build "PROFILE=${PROFILE}" "OUTPUT_FILE=${BIN_OUTPUT_REL}"
        ) || die "PAXX build failed - see its output above. This script only \
drives it; the failure is inside dev.sh/make, not in the overlay staging \
that already succeeded."

        BUILT_BIN="${PAXX_FORK}/${BIN_OUTPUT_REL}"
        [ -f "$BUILT_BIN" ] || die "build reported success but \
${BUILT_BIN} does not exist - PAXX's OUTPUT_FILE handling may have changed."

        mkdir -p "$OUT_DIR"
        FINAL_BIN="${OUT_DIR}/multiace-${ARTIFACT_VERSION}-${PROFILE}.bin"
        cp "$BUILT_BIN" "$FINAL_BIN"
        ( cd "$OUT_DIR" && sha256sum "$(basename "$FINAL_BIN")" \
            > "$(basename "$FINAL_BIN").sha256" )
        say "wrote $(du -h "$FINAL_BIN" | awk '{print $1}') -> ${FINAL_BIN}"
        say "this is the file to open from the printer's update screen."
    fi
fi

say "done"
