#!/bin/sh
# Counter snapshot watcher for Wi-Fi stall diagnosis. BusyBox-compatible.
# Usage: ./stall_watch.sh [COUNT] [INTERVAL_S]
# Default: 215 snapshots x 2s = 430s. Self-counted so it exits on its own
# (a remote `while true` would survive a killed SSH and leak as an orphan).
# Output: one "SNAP ..." line per interval, parseable by analyze_stall.py.
COUNT=${1:-215}
INTERVAL=${2:-2}
IFACE=${WLAN_IFACE:-wlan0}
i=0
while [ $i -lt $COUNT ]; do
  up=$(cut -d' ' -f1 /proc/uptime)
  load=$(cut -d' ' -f1 /proc/loadavg)
  dm=$(dmesg 2>/dev/null | wc -l)
  rx=$(cat /sys/class/net/$IFACE/statistics/rx_bytes 2>/dev/null)
  tx=$(cat /sys/class/net/$IFACE/statistics/tx_bytes 2>/dev/null)
  rxd=$(cat /sys/class/net/$IFACE/statistics/rx_dropped 2>/dev/null)
  sig=$(iw dev $IFACE link 2>/dev/null | awk '/signal:/{print $2}')
  if iw dev $IFACE link 2>/dev/null | grep -q Connected; then conn=Y; else conn=N; fi
  echo "SNAP up=$up load=$load rx=$rx tx=$tx rxd=$rxd dmesg=$dm sig=$sig conn=$conn"
  i=$((i+1))
  sleep $INTERVAL
done
