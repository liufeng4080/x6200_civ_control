---
name: x6200-wifi-stall-debug
description: Diagnose periodic ICMP stalls on a Linux Wi-Fi host caused by a secondary interface's off-channel scans freezing the shared PHY. Use when ping shows regular multi-second gaps with a queue-drain RTT signature, especially on BusyBox/embedded radios. Do not use for one-off loss or wired links.
---

# X6200 Wi-Fi Intermittent Stall Debug

Evidence-first workflow for the X6200 radio (Buildroot/BusyBox, rtl8723du staging driver, wlan0 + wlan1 sharing phy#0). Do not change radio config until a hypothesis is evidenced; then apply the minimal fix and A/B compare. Full worked example lives in `debug-radio-ping-stalls.md` at the repo root.

## Signature of this bug

Ping to the radio shows regular gaps (~5-10 s) where replies stop, then arrive in a burst with RTT decreasing by ~1000 ms per packet (queue drain). The gap period is constant and matches the secondary interface's scan interval (~63 s observed here). Both directions (host-to-radio and radio-to-host) stall together. The link never drops, signal is stable, and the staging driver logs nothing to dmesg.

## Workflow (read-only until evidenced)

### 1. Bidirectional 1 Hz capture with a time anchor
Run a long capture (at least 5x the suspected period; 420 s covers ~6-7 of the 63 s windows):
- host -> radio and host -> gateway: `stdbuf -oL ping -D -i 1 -c N <ip>`. `stdbuf -oL` forces line flushing — without it, redirected ping output is block-buffered and lost if the wrapper dies before ping exits.
- radio -> host: over SSH, `ping -i 1 -c N <host_ip>`. BusyBox ping rejects `-O` and fractional `-i`.
- Record an anchor at start: local `date +%s.%N` paired with radio uptime (`cut -d' ' -f1 /proc/uptime`). Reverse ping seq N maps to t=N seconds after start, so `radio_uptime ~= start_radio + seq`.

### 2. Target-side counter snapshots (watcher)
Run `scripts/stall_watch.sh` on the radio every 2 s. It logs wlan0 rx/tx bytes, rx_dropped, dmesg line count, signal, link state. The decisive signal of a PHY freeze is **>= 2 consecutive snapshots with delta-rx == 0 AND delta-tx == 0** while a stall is ongoing. If counters keep incrementing during a stall, the cause is AP-side unicast delivery, not a local PHY freeze.

### 3. Global `iw event` monitor
`iw event -t` listens to the whole PHY (this build takes no interface argument). Tag events as wlan0/wlan1 from the line text. Look for `scan started` on the secondary interface listing channels across the band — that is an off-channel scan that freezes the shared PHY.

### 4. Correlate (the decisive test)
Run `scripts/analyze_stall.py <capture_dir>`. It:
- Finds stall bursts (consecutive RTT > 1000 ms, decreasing) and checks their spacing is constant and equals the scan period.
- Overlays watcher snapshots on each burst and flags counter-freeze windows.
- Lists `iw event` scan lines and checks whether a scan lands on the predicted stall grid (burst0_epoch + k * period).
All three together — period match + counter freeze + scan on the predicted grid — confirm the shared-PHY-scan root cause.

## Root cause (confirmed on X6200)
wlan1 is disconnected and unused, but NetworkManager manages it and wpa_supplicant runs a periodic off-channel scan every ~63 s. wlan0 and wlan1 share phy#0, so the scan freezes the entire PHY for ~4-8 s. wlan0 packets queue and release when the scan finishes.

Caveat: a single active `iw dev wlan1 scan` may return cached results in ~1 ms without going off-channel, so it will NOT reproduce the stall. Do not take that as evidence against the scan hypothesis. The real periodic scans do go off-channel.

## Minimal fix (reversible)
wlan1 is unused. Disable it:
```
nmcli device set wlan1 managed no
ip link set wlan1 down
```
Verify wlan0 is unaffected (still connected, SSH alive). wlan1 shows no `UP` flag and `nmcli device` reports `unmanaged`.

### Persist across reboot
The runtime change is lost on reboot. Add `/etc/NetworkManager/conf.d/99-unmanaged-wlan1.conf`:
```
[keyfile]
unmanaged-devices=interface-name:wlan1
```
Then `nmcli connection reload` (this Buildroot NM does not support `nmcli general reload`). The rootfs is ext4 on eMMC (rw), so the file survives reboot. Roll back by deleting the file and reloading. After a future reboot, confirm `nmcli device | grep wlan1` still says `unmanaged`.

## A/B verification
Re-run the same 1 Hz bidirectional capture + watcher + `iw event` post-fix. Expect max RTT to drop from ~10 s to < 500 ms, zero > 1000 ms bursts, zero counter-freeze windows, zero wlan1 scan events. Report pre/post numbers side by side.

## BusyBox / environment gotchas (do not relearn)
- No `timeout` command on the radio — apply `timeout` on the LOCAL ssh wrapper, not inside the remote string (a remote `timeout` gives exit 127).
- `pgrep` is missing in non-interactive SSH (PATH issue); use `ps` to check liveness. Never relaunch a watcher based on a failed `pgrep` — it causes two writers to interleave into one log (negative deltas). Use a separate log file for any relaunch.
- Local `ping` redirected to a file is fully block-buffered by glibc; prefix `stdbuf -oL` or output is lost if the wrapper dies before ping exits.
- A remote `while true` loop survives a killed SSH (local `timeout` kills ssh, not the remote loop). Bound every remote loop with a self-counter (`i=0; while [ $i -lt N ]; ...`) and kill by exact PID afterward using `ps`.
- `iw dev wlan0 station dump` produces no output on this build; debugfs is not mounted. Rely on `/sys/class/net/wlan0/statistics/*` and `/proc/net/wireless` instead.
- `iw event -t` takes no interface arg; monitor globally and tag from line text.
- Keep the SSH askpass in `/tmp` (mode 700). Never write the radio password into the repo.

## Cleanup (before declaring done)
- Kill any remote watcher by exact PID; verify with `ps` that no `while` / `stall_watch` loops are orphaned.
- Stop the Debug Server; delete the askpass, `/tmp` evidence dirs, and analysis scripts.
- Keep `debug-radio-ping-stalls.md` in the repo as the record.
