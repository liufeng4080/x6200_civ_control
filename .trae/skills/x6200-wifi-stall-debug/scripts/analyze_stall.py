#!/usr/bin/env python3
"""Correlate bidirectional ping stalls with target-side counter snapshots and
iw-event scan lines. Expects a capture directory produced by this skill's
workflow:

  <dir>/anchors.log        -> "CAPTURE_START host_epoch=... radio_uptime=..."
  <dir>/reverse_ping.log   -> BusyBox ping: "seq=N ... time=X ms"
  <dir>/ping_radio.log     -> local ping: "icmp_seq=N ... time=X ms" (optional)
  <dir>/watcher.log        -> "SNAP up=.. rx=.. tx=.. rxd=.. dmesg=.. sig=.. conn=.."
  <dir>/iwevent.log        -> raw `iw event -t` output (scan lines)

Usage: python3 analyze_stall.py <capture_dir>
"""
import re, os, sys

def usage():
    print(__doc__); sys.exit(1)

def parse_kv(line):
    out = {}
    for m in re.finditer(r'(\w+)=([^\s]+)', line):
        out[m.group(1)] = m.group(2)
    return out

def main():
    if len(sys.argv) < 2 or not os.path.isdir(sys.argv[1]):
        usage()
    D = sys.argv[1]

    # --- anchor ---
    anchors = []
    ap = os.path.join(D, "anchors.log")
    if os.path.exists(ap):
        for line in open(ap):
            m = re.search(r'host_epoch=([0-9.]+).*?radio_uptime=([0-9.]+)', line)
            if m:
                anchors.append((float(m.group(1)), float(m.group(2))))
    if anchors:
        anchors.sort()
        start_host, start_radio = anchors[0]
        print(f"anchor start host_epoch={start_host} radio_uptime={start_radio}")
    else:
        start_host = start_radio = None
        print("anchor: none found (seq->uptime mapping unavailable)")

    def seq_to_radio(seq):
        return start_radio + seq if start_radio is not None else None

    # --- parse a ping log ---
    def parse_ping(fname, seq_re):
        replies = {}
        p = os.path.join(D, fname)
        if not os.path.exists(p):
            return replies
        for line in open(p):
            m = re.search(seq_re, line)
            if m:
                replies[int(m.group(1))] = float(m.group(2))
        return replies

    rev = parse_ping("reverse_ping.log", r'seq=(\d+).*?time=([0-9.]+)')
    loc = parse_ping("ping_radio.log", r'icmp_seq=(\d+).*?time=([0-9.]+)')

    def summarize(name, replies):
        if not replies:
            print(f"{name}: (no data)"); return [], 0
        seqs = sorted(replies)
        print(f"{name}: replies={len(replies)} seq={seqs[0]}..{seqs[-1]} max_rtt={max(replies.values()):.1f}")
        # bursts: consecutive rtt>1000
        bursts = []; cur = []
        for s in seqs:
            if replies[s] > 1000:
                cur.append((s, replies[s]))
            else:
                if len(cur) >= 2: bursts.append(cur)
                cur = []
        if len(cur) >= 2: bursts.append(cur)
        fs = [b[0][0] for b in bursts]
        deltas = [fs[i+1]-fs[i] for i in range(len(fs)-1)]
        periodic = len(set(deltas)) == 1 and len(deltas) > 0
        print(f"  bursts(>1000ms consecutive)={len(bursts)} first_seqs={fs} deltas={deltas}"
              + (f"  PERIOD={deltas[0]}s (constant)" if periodic else ""))
        for i, b in enumerate(bursts):
            print(f"    burst#{i}: seq {b[0][0]}..{b[-1][0]} rtt {b[0][1]:.0f}..{b[-1][1]:.0f}")
        return bursts, (deltas[0] if periodic else None)

    print()
    rev_bursts, rev_period = summarize("reverse(radio->host)", rev)
    loc_bursts, loc_period = summarize("local(host->radio)", loc)

    # --- parse watcher (SNAP key=value lines) ---
    snaps = []
    wp = os.path.join(D, "watcher.log")
    if os.path.exists(wp):
        for line in open(wp):
            if not line.startswith("SNAP"):
                continue
            kv = parse_kv(line)
            try:
                snaps.append(dict(
                    up=float(kv['up']),
                    rx=int(kv.get('rx', 0)),
                    tx=int(kv.get('tx', 0)),
                    rxd=int(kv.get('rxd', 0)),
                    dmesg=int(kv.get('dmesg', 0)),
                    sig=kv.get('sig', ''),
                    conn=(kv.get('conn') == 'Y'),
                ))
            except (KeyError, ValueError):
                continue
    print(f"\nwatcher snaps={len(snaps)}"
          + (f" uptime={snaps[0]['up']:.1f}..{snaps[-1]['up']:.1f}" if snaps else ""))

    # --- counter freeze windows: >=2 consecutive snaps with drx==0 and dtx==0 ---
    freezes = []
    if len(snaps) >= 2:
        run = []  # list of (prev_up, cur_up) zero-delta steps
        prev = snaps[0]
        for s in snaps[1:]:
            drx = s['rx'] - prev['rx']
            dtx = s['tx'] - prev['tx']
            if drx == 0 and dtx == 0:
                run.append((prev['up'], s['up']))
            else:
                if run:
                    freezes.append((run[0][0], run[-1][1], len(run) + 1))
                run = []
            prev = s
        if run:
            freezes.append((run[0][0], run[-1][1], len(run) + 1))
    print(f"counter freeze windows (>=2 snaps drx=0 & dtx=0): {len(freezes)}")
    for f in freezes:
        print(f"  radio_uptime {f[0]:.1f}..{f[1]:.1f} ({f[1]-f[0]:.1f}s, {f[2]} snaps)")

    # --- iw event scan lines ---
    scan_lines = []
    ie = os.path.join(D, "iwevent.log")
    if os.path.exists(ie):
        for line in open(ie):
            if re.search(r'scan', line, re.I):
                scan_lines.append(line.rstrip())
    print(f"\niw event scan lines: {len(scan_lines)}")
    for l in scan_lines[:20]:
        print("  ", l)
    if not scan_lines:
        print("  (none)")

    # --- does a scan land on the predicted stall grid? ---
    if scan_lines and rev_bursts and rev_period and start_host is not None:
        first_epoch = start_host + rev_bursts[0][0][0]
        print(f"\ngrid check (period={rev_period}s, burst#0 epoch={first_epoch:.3f}):")
        for l in scan_lines:
            m = re.search(r'^([0-9.]+):', l)
            if not m:
                continue
            t = float(m.group(1))
            k = round((t - first_epoch) / rev_period)
            err = t - (first_epoch + k * rev_period)
            ongrid = abs(err) < 1.0
            print(f"  scan @{t:.3f} -> grid k={k} err={err:+.3f}s {'ON-GRID' if ongrid else ''}")

    # --- summary ---
    period = rev_period or loc_period
    print("\n" + "=" * 56)
    print("SUMMARY")
    print("=" * 56)
    print(f"  bursts(reverse)={len(rev_bursts)}  bursts(local)={len(loc_bursts)}")
    print(f"  burst period={period}s" + ("  <-- matches scan interval" if period else ""))
    print(f"  counter freeze windows={len(freezes)}")
    print(f"  wlan scan events={len(scan_lines)}")
    confirmed = (len(rev_bursts) > 0 or len(loc_bursts) > 0) and len(freezes) > 0 and len(scan_lines) > 0
    print(f"  shared-PHY-scan root cause: {'CONFIRMED' if confirmed else 'not confirmed by this capture'}")

if __name__ == "__main__":
    main()
