#!/bin/sh
# fix-ssh-timeout.sh
# Verhindert dass der Cerbo durch haengende SSH-Sessions blockiert wird.
#
# Installation:
#   scp rc/fix-ssh-timeout.sh cerbo:/data/
#   ssh cerbo "sh /data/fix-ssh-timeout.sh"

set -e

# Watchdog-Skript erstellen
cat > /data/kill_stale_ssh.sh << 'EOF'
#!/bin/sh
MAIN_PID=$(cat /var/run/dropbear.pid 2>/dev/null || echo "0")
for dir in /proc/[0-9]*; do
    pid=$(basename "$dir")
    [ "$pid" = "$MAIN_PID" ] && continue
    comm=$(cat "$dir/comm" 2>/dev/null || true)
    [ "$comm" = "dropbear" ] || continue
    start=$(stat -c %Y "$dir" 2>/dev/null || echo 0)
    now=$(date +%s)
    age=$(( now - start ))
    if [ "$age" -gt 180 ]; then
        logger -t ssh-watchdog "Killing stale dropbear pid=$pid age=${age}s"
        kill "$pid" 2>/dev/null || true
    fi
done
EOF
chmod +x /data/kill_stale_ssh.sh
echo "[OK] Watchdog erstellt"

# Cron-Job jede Minute
(crontab -l 2>/dev/null | grep -v kill_stale_ssh; echo "* * * * * /data/kill_stale_ssh.sh") | crontab -
echo "[OK] Cron-Job aktiv"

# Sofort ausfuehren
sh /data/kill_stale_ssh.sh
echo "[OK] Watchdog laeuft"
