#!/bin/sh
set -e
WEB_SRC=/home/lava/multiace/web
WEB_DEST=/home/lava/multiace_web
NGINX_DROPIN_FLUIDD=/etc/nginx/fluidd.d/multiace-web.conf
NGINX_DROPIN_CONFD=/etc/nginx/conf.d/multiace-web.conf
INIT_SCRIPT=/etc/init.d/S98multiace-web
LOG=/tmp/multiace_install_web.log
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [multiace-web] $1" | tee -a "$LOG"; }
if [ "$(id -u)" -ne 0 ]; then
    log "ERROR: this script must be run as root."
    log "From your workstation: ssh root@<printer-ip> 'bash $0'"
    exit 2
fi
if [ "${1:-}" = "--remove" ]; then
    log "removing multiACE Web service"
    [ -x "$INIT_SCRIPT" ] && "$INIT_SCRIPT" stop 2>/dev/null || true
    rm -f "$INIT_SCRIPT" "$NGINX_DROPIN_FLUIDD" "$NGINX_DROPIN_CONFD"
    SM_FLUIDD_CONF=/etc/nginx/sites-available/fluidd
    if [ -f "${SM_FLUIDD_CONF}.bak.multiace" ]; then
        mv "${SM_FLUIDD_CONF}.bak.multiace" "$SM_FLUIDD_CONF"
        log "  reverted $SM_FLUIDD_CONF from backup"
    fi
    rm -rf "$WEB_DEST"
    if pidof nginx >/dev/null 2>&1; then
        nginx -s reload 2>/dev/null || true
    fi
    log "done"
    exit 0
fi
log "=== multiACE Web install ==="
if [ ! -d "$WEB_SRC/backend" ] || [ ! -f "$WEB_SRC/backend/main.py" ]; then
    log "ERROR: web sources missing at $WEB_SRC/backend"
    log "       Reflash the PAXX image or re-run install_multiace.sh."
    exit 1
fi
log "Copying service files -> $WEB_DEST"
mkdir -p "$WEB_DEST/backend" "$WEB_DEST/frontend" "$WEB_DEST/i18n"
cp -r "$WEB_SRC/backend/."  "$WEB_DEST/backend/"
cp -r "$WEB_SRC/frontend/." "$WEB_DEST/frontend/"
if [ -d /home/lava/multiace/i18n ]; then
    cp -r /home/lava/multiace/i18n/. "$WEB_DEST/i18n/"
fi
rm -rf "$WEB_DEST/backend/__pycache__"
chown -R lava:lava "$WEB_DEST"
log "Installing Python deps (--user, as lava)"
if su - lava -c "command -v pip3 >/dev/null"; then
    su - lava -c "pip3 install --user --upgrade -r '$WEB_DEST/backend/requirements.txt'" \
        >>"$LOG" 2>&1 || log "WARN: pip install returned non-zero (see $LOG)"
else
    log "ERROR: pip3 not on lava's PATH - install python3-pip first"
    exit 1
fi
log "Ensuring log file exists"
mkdir -p /home/lava/printer_data/logs
touch /home/lava/printer_data/logs/multiace_web.log
chown lava:lava /home/lava/printer_data/logs/multiace_web.log
log "Installing nginx drop-in"
SM_FLUIDD_CONF=/etc/nginx/sites-available/fluidd
if [ -d /etc/nginx/fluidd.d ]; then
    cp "$WEB_SRC/deploy/multiace-web.nginx.conf" "$NGINX_DROPIN_FLUIDD"
    log "  -> $NGINX_DROPIN_FLUIDD"
elif [ -f "$SM_FLUIDD_CONF" ]; then
    if grep -q '/multiace/' "$SM_FLUIDD_CONF"; then
        log "  $SM_FLUIDD_CONF already has /multiace/ - skipping patch"
    else
        cp "$SM_FLUIDD_CONF" "${SM_FLUIDD_CONF}.bak.multiace"
        TMP_LOC="$(mktemp)"
        sed '/auth_request/d' \
            "$WEB_SRC/deploy/multiace-web.nginx.conf" \
            | sed 's/^/    /' \
            > "$TMP_LOC"
        awk -v insert_file="$TMP_LOC" '
            !done && /^    location \/ \{$/ {
                while ((getline line < insert_file) > 0) print line
                close(insert_file)
                print ""
                done = 1
            }
            { print }
        ' "${SM_FLUIDD_CONF}.bak.multiace" > "$SM_FLUIDD_CONF"
        rm -f "$TMP_LOC"
        log "  -> patched $SM_FLUIDD_CONF (backup at ${SM_FLUIDD_CONF}.bak.multiace)"
    fi
else
    mkdir -p /etc/nginx/conf.d
    cp "$WEB_SRC/deploy/multiace-web.nginx.conf" "$NGINX_DROPIN_CONFD"
    log "  -> $NGINX_DROPIN_CONFD (no fluidd.d/ and no sites-available/fluidd)"
fi
log "Installing init script"
cp "$WEB_SRC/deploy/S98multiace-web" "$INIT_SCRIPT"
chmod +x "$INIT_SCRIPT"
log "Starting service"
"$INIT_SCRIPT" stop 2>/dev/null || true
"$INIT_SCRIPT" start
log "Reloading nginx"
if pidof nginx >/dev/null 2>&1; then
    if nginx -t >>"$LOG" 2>&1; then
        nginx -s reload 2>/dev/null && log "  nginx reloaded"
    else
        log "  WARN: nginx -t failed - drop-in installed but not active"
    fi
else
    log "  WARN: nginx not running - multiace-web reachable only on uvicorn port 7126"
fi
log "OK - multiACE Web available at http://<printer-ip>/multiace/"
log "Remove later via: bash $0 --remove"
