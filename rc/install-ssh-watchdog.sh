#!/bin/sh
# Installiert den SSH-Watchdog als Daemontools-Service auf Venus OS.
#
# Ausfuehrung:
#   scp -r rc/ssh-watchdog cerbo:/data/ssh-watchdog
#   scp rc/install-ssh-watchdog.sh cerbo:/data/
#   ssh cerbo "sh /data/install-ssh-watchdog.sh"

set -e

SVC_SRC="/data/ssh-watchdog"
SVC_DST="/service/ssh-watchdog"

# Alten Cron-Job entfernen (falls vorhanden)
(crontab -l 2>/dev/null | grep -v kill_stale_ssh) | crontab - 2>/dev/null || true
rm -f /data/kill_stale_ssh.sh /data/fix-ssh-timeout.sh

# Service installieren
if [ -d "$SVC_DST" ]; then
    svc -d "$SVC_DST" 2>/dev/null || true
    rm -rf "$SVC_DST"
fi

cp -r "$SVC_SRC" "$SVC_DST"
chmod +x "$SVC_DST/run" "$SVC_DST/log/run"
mkdir -p /var/log/ssh-watchdog

echo "[OK] ssh-watchdog Service installiert"
echo "[OK] supervise startet ihn automatisch"

# Pruefen ob er laeuft
sleep 2
if svok "$SVC_DST" 2>/dev/null; then
    echo "[OK] Service laeuft"
else
    echo "[WARN] Service noch nicht gestartet - svscan braucht evtl. einen Moment"
fi
