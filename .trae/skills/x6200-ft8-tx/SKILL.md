---
name: x6200-ft8-tx
description: Transmit CW or FT8 over X6200 LAN audio with UTC slot timing, PTT safety, and decode checks. Use when sending FT8 CQ or CW with the auto_x6200 client, or debugging radio TX audio.
---

# X6200 LAN FT8/CW Transmit

Operate the `auto_x6200` client (Python 3 stdlib only) to send CW or FT8 through the radio's WFSERVER LAN audio path. Apply the timing and PTT rules below; they were derived from on-air decode tests.

## Preflight

1. Confirm the radio answers on the LAN: `ping -c 3 192.168.31.221`. The CLI default host `.99` is stale, always pass `--host 192.168.31.221`.
2. Check the WFSERVER audio backend on the radio: `grep AudioSystem /etc/xgradio/wfserver.ini`. It must read `AudioSystem=1`, which routes TX audio through `mixcapture`/`mixplayback`. With `AudioSystem=0` the server writes directly to the hardware device, and the 12.5 ms packet cadence then underruns under any scheduling jitter: audible gaps, `skipped` counts above zero and bursts of retransmit requests. A reflash or a hand edit can reset it to `0`, so re-check after any firmware work. It takes effect after wfserver restarts.
3. Ensure no local `wfview` is running; the client refuses to start otherwise.
4. The radio already carries persistent settings for USB power management and Bluetooth, installed with SSH `root@192.168.31.221`:
   - `/etc/udev/rules.d/60-x6200-usb-nosuspend.rules` sets `power/control=on` and `autosuspend_delay_ms=-1` for the RTL8723DU dongle
   - `/etc/udev/rules.d/60-x6200-bt-off.rules` soft-blocks Bluetooth
   - `/etc/modprobe.d/60-x6200-wifi-nopm.conf` loads the driver with `rtw_power_mgnt=0 rtw_ips_mode=0 rtw_enusbss=0`
   - `/etc/init.d/S50x6200-pm` re-applies all of the above after the network starts, because this firmware does not replay udev `add` events for devices enumerated before udevd

   A factory reflash wipes these files and resets the driver to `rtw_power_mgnt=2 rtw_ips_mode=1` (inactive power save). That alone drops about 75 percent of 1400-byte bursts and stalls LAN audio after idle. Re-apply at runtime without rebooting:
   ```bash
   iw dev wlan0 set power_save off
   echo 0 > /sys/module/8723du/parameters/rtw_power_mgnt
   echo 0 > /sys/module/8723du/parameters/rtw_ips_mode
   nmcli connection modify wifi-8 802-11-wireless.powersave 2
   ```

   `power/control` still reads `auto` after boot since the driver re-enables runtime PM; the `-1` delay is what prevents suspension and is left untouched by the driver. Verify with `cat /sys/bus/usb/devices/1-1/power/autosuspend_delay_ms`, which must print `-1`. Only if that file is missing or differs, re-apply manually:
   ```bash
   echo -1 > /sys/bus/usb/devices/1-1/power/autosuspend_delay_ms
   echo on > /sys/bus/usb/devices/1-1/power/control
   rfkill block bluetooth
   ```
4. Verify tight-packet delivery before a real test with `ping -s 1350 -c 40 192.168.31.221`. Loss must be 0 percent; near-100 percent loss means the USB autosuspend fix above was reverted, and occasional 70-90 percent bursts mean Wi-Fi power save came back.

## Commands

```bash
cd /home/liufeng/Documents/trae_projects/auto_x6200

# Offline check: builds PCM, writes WAV, never opens the network
python3 x6200.py --ft8 "CQ BH1WQR OM89" --wav /tmp/ft8.wav

# U-DIG set plus 01/01/01 readback only
python3 x6200.py --host 192.168.31.221 --mode-only

# Real FT8 transmission on the next UTC slot
python3 x6200.py --host 192.168.31.221 --ft8 "CQ BH1WQR OM89" --tx

# CW
python3 x6200.py --host 192.168.31.221 "TEST" --tx
```

Only `CQ BH1WQR OM89` is accepted for FT8; any other text raises `ValueError` by design. Do not add a general FT8 encoder without an independently verified tone table.

## Timing rules

| Constant | Value | Purpose |
| --- | --- | --- |
| `FT8_START_LEAD_NS` | 8 s | Minimum gap from now to the chosen slot |
| `FT8_PTT_LEAD_NS` | 0 s | Request PTT at the slot boundary, not in advance |
| `PTT_ACK_WATCHDOG_NS` | 4 s | Abort and turn PTT off if neither FB ACK nor TX readback arrives |
| `CIV_ACK_TIMEOUT` | 2 s | Also schedules one non-blocking FT8 PTT ON retry |
| `AUDIO_SAMPLES_PER_TICK` | 600 | 12.5 ms per datagram, 1224 bytes on the wire |

- The FT8 path calls `ptt_on_fire()` at the slot boundary and starts the 79 symbols immediately. Do not move blocking `set_ptt(True)` back into this path: waiting for ACK/readback was what previously shifted DT to about 2.1 s.
- While PTT confirmation is pending, `service_background()` drains CI-V but does not send periodic TX-state queries. If still unconfirmed after 2 s, it sends one PTT ON retry without pausing audio. ACK or TX readback disarms the watchdog and enables `tx_monitor`.
- The first PTT ON commonly receives ACK only after the background retry, and TX readback can change from RX to TX later still. These protocol delays do **not** prove that RF started late. The 2026-11-22 on-air test had ACK at about +2.1 s and TX readback at about +3.1 s, yet the receiver measured DT = +0.1 s at −6 dB.
- Keep the periodic PTT state readback inside `service_background()` after confirmation (`tx_monitor`, `1C 00`). Removing it reintroduces the 6 second audio stall while the carrier stays keyed.
- If no ACK or TX readback arrives within 4 s, the watchdog raises `PTTError`; the `finally` cleanup sends PTT OFF and requires RX confirmation.

## Expected log sequence

```
FT8 目标时隙：... UTC
PTT ON 发送（不等 ACK）：...
FT8 UTC 时隙开始：立即 PTT 并发送 79 个符号（不等 ACK，4 秒看门狗）
PTT ON 未确认，后台补发一次（不阻塞音频）  (at about slot + 2 s; absent if the first ACK arrives)
PTT 生效确认（ACK=True，TX回读=False），看门狗解除
CI-V PTT 状态回读：RX                     (a transitional readback is possible)
CI-V PTT 状态回读：TX                     (repeats about once per second)
PTT OFF 已获 FB，电台 TX 状态确认：RX
网络音频发送完成：packets=... late=... skipped=0 errors=0
```

Do not infer DT from the ACK or TX-readback timestamps. Confirm DT at the receiver.

Success requires `skipped=0`, `errors=0`, and a final RX confirmation.

## Verification

Network completion is not success. Ask the operator to confirm decode on WSJT-X or a second receiver:

- Message decoded as `CQ BH1WQR OM89`
- The verified slot-start PTT configuration measured DT = +0.1 s and −6 dB on 2026-11-22
- Audible burst about 12.6 s long, not truncated to about 6 s

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| Audio stops after about 6 s, PTT stays TX | Missing periodic PTT readback, or radio USB power management re-enabled | Keep `tx_monitor`, re-apply preflight step 3 |
| PTT never acknowledged, status stays RX | Post-reboot zombie session (`conninfo` shows `busy=1`) | Power-cycle the radio, then retry |
| Many retransmit requests during TX | Wi-Fi link degraded | Re-run preflight steps 3 and 4 |
| DT about 2 s | The FT8 path regressed to blocking PTT confirmation or an old mirror is running | Confirm FT8 uses `ptt_on_fire()` with zero lead; do not add a pre-slot PTT delay |
| ACK arrives only after the 2 s retry but received DT is about 0.1 s | Observed normal radio behavior; protocol confirmation is delayed, RF is not | No action; keep the background retry and 4 s watchdog |
| `U-DIG 回读为 01/00/01` right after `CI-V open` | First `26 00 01 01 01` frame is dropped on a freshly opened CI-V channel, especially after a reflash | Nothing to do: `set_udig` retries up to 3 times with a 0.3 s gap and still refuses PTT unless it reads back `01/01/01` |
| Radio still keyed after an error | CI-V lost during the run | Reconnect once and send PTT OFF, then confirm RX |

## Tests

```bash
cd /home/liufeng/Documents/trae_projects/auto_x6200
python3 -m unittest
```

All 40 tests must pass before and after any timing change.
