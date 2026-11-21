# Debug Session: radio-ping-stalls
- **Status**: [RESOLVED]
- **progress_status**: 已解决。根因 CONFIRMED + 修复已实施并 A/B 验证：禁用 wlan1 后，63 s 阻塞、wlan0 计数器冻结、wlan1 扫描全部消失，max RTT 从 ~10 s 降至 150 ms（本机→电台）/ 490 ms（电台→本机），0 突发。已持久化（/etc/NetworkManager/conf.d/99-unmanaged-wlan1.conf）。方法论已沉淀为项目 skill：x6200-wifi-stall-debug。待用户决定是否提交；建议重启电台验收持久化。
- **Issue**: 电台 192.168.31.221 在 ping 时间歇出现约 10 秒无响应；预期局域网 ICMP 持续稳定返回。
- **Evidence Source**: HTTP Debug Server（启动与健康/探针检查后确认）
- **Evidence Directory**: /tmp/x6200-civ-control-radio-ping-stalls
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: /tmp/x6200-civ-control-radio-ping-stalls/trae-debug-log-radio-ping-stalls.ndjson

## Reproduction Steps
1. runId=pre-fix，以带时间戳的 ping 同时监测电台与默认网关。
2. 采集本机路由、ARP、无线链路状态，并通过 SSH 只读采集电台电源管理、驱动、系统及内核日志。
3. 将约 10 秒的 ICMP 缺口与同一时间的链路、负载和配置事件对齐。
4. 以用户原始 1 Hz 场景做双向 ping，并同步记录电台内部 heartbeat，区分本机到电台与电台到本机的数据路径。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | USB Wi-Fi runtime suspend/autosuspend 被重新启用，唤醒导致阻塞 | Low | Low | REJECTED（针对已捕获长事件）：180/180 次内部采样均为 active，runtime_suspended_time=0，autosuspend_delay_ms=-1，active_time 持续增加 |
| B | NetworkManager Wi-Fi power save 或 8723du IPS 重新启用 | Low | Low | 已检查的配置路径不成立：Power save=off、NM powersave=disable，rtw_power_mgnt/rtw_ips_mode/rtw_enusbss/rtw_low_power/rtw_lps_level 均为 0；驱动/固件内部异常仍归入 D 继续区分 |
| C | 无线重连、AP/ARP/路由异常导致电台不可达 | Medium | Medium | INCONCLUSIVE：长阻塞中 ARP 经 DELAY→PROBE→FAILED→INCOMPLETE，恢复后仍是原 MAC；不支持 IP/MAC 冲突，ARP 失败更像不应答的结果。电台无线数据路径仍未排除 |
| D | 电台 CPU/软中断/驱动卡顿使 ICMP 延迟 | High | Medium | 扫描子假设 **CONFIRMED**：wlan1（与 wlan0 共享 phy#0）每 63 s 发起离信道全频段扫描，冻结 phy → wlan0 计数器出现 ~4 s 连续零增量（drx=0 且 dtx=0），反向 ping 同一时刻排队后突发释放；confirm 采集在预测阻塞时刻（63 s 网格）直接观测到 `wlan1 scan started`。整机/用户空间停顿子假设仍 REJECTED。⚠️ 先前"扫描子假设 REJECTED"基于一次返回缓存结果的非离信道主动扫描（1.7 ms、未真正离信道），不构成反证，已修正 |
| E | 本机到 AP 的链路问题，而非电台本身 | Low | Low | REJECTED：两轮同步采样网关均 900/900、0 次超时，而电台最大 RTT 分别 8993/10826 ms；本机信号、错误计数和 journal 无同步异常 |
| F | rtl8723du 驱动/固件或 USB 数据面的间歇性静默停顿（staging 驱动不记日志） | Medium | Medium | OPEN（残留可能）：wlan1 离信道扫描已是首要证据根因；F1 `rtw_smart_ps=2`/`rtw_lps_chk_by_tp=1` 运行时可写，仅在禁用 wlan1 后阻塞仍残留时作为二级杠杆做 A/B 验证 |
| G | AP 对该客户端的单播队列/交付异常（airtime fairness、客户端休眠缓冲、按客户端限速） | Low | Medium | OPEN 但被削弱：600 s 对齐采集显示阻塞瞬间 wlan0 rx/tx 计数器 ~4 s 连续冻结（phy 停顿），不支持纯 AP 侧单播交付异常；AP 侧日志仍需管理员凭据（用户门控），暂不具备 |

## Log Evidence
- NDJSON lines 5-89：第一轮长事件中，旧 replies 在恢复后集中返回，seq=174 RTT=8993 ms，后续逐包递减，符合停顿期间排队、恢复后集中释放。
- NDJSON lines 286-288：第一轮网关 replies=900、timeouts=0；电台 replies=900、timeouts=105、max_rtt=8993 ms。
- NDJSON lines 467-550、616-623：第二轮再次出现长阻塞，最大 RTT=10826 ms；期间 radio.internal heartbeat 仍连续，USB=active、wlan_operstate=up、carrier=1。
- NDJSON lines 481、520、539、543、619：同一长事件内 ARP 从 DELAY/PROBE 到 FAILED/INCOMPLETE，再恢复原 MAC 14:0a:02:70:89:76；未观察到 MAC 漂移。
- NDJSON line 685：内部采样 samples=180、max_sample_gap_s=1.03、usb_runtime_states=[active]、returncode=0；长阻塞期间没有整机 10 秒调度停顿。
- NDJSON lines 687-690：第二轮网关 replies=900、timeouts=0、max_rtt=152 ms；电台 replies=880、timeouts=193、high_latency=105、max_rtt=10826 ms。
- NDJSON lines 701-702：60 秒双向 1 Hz 测试中，本机→电台 56/60、max_rtt=1329.850 ms；电台→本机 53/60、max_rtt=320.073 ms。两个方向都有丢包，但未复现 10 秒事件，不能据此判断方向性。
- NDJSON line 704：USB/Wi-Fi 省电和驱动参数基线已记录；所有读取均为只读，未修改电台配置。
- NDJSON lines 705-720、1106-1121：第三轮原始 1 Hz 场景复现两次长事件；第二次 seq=213 的 RTT=7505 ms，随后旧 replies 逐包递减释放。
- NDJSON lines 1127-1137：第二次长事件同时影响电台→本机方向，seq=204 的 RTT=6515.086 ms；两个方向在同一恢复时刻集中返回，排除了单纯本机→电台下行 ICMP 丢失。
- NDJSON lines 1136-1155：故障期间电台仍逐秒生成 samples，uptime 连续，USB=active、BSSID/carrier 不变；这些 samples 在恢复后于约 200 ms 内集中送达，和 ICMP 的集中释放一致。
- NDJSON lines 1310、1312、1332：300 秒同步结果，网关 300/300、0 超时；本机→电台 300/300、max_rtt=7505 ms；电台→本机 300/300、max_rtt=6515.086 ms。
- NDJSON line 1363：300 次内部采样的 max_remote_epoch_gap_s=2，但本机 SSH max_receive_gap_s=14.639；BSSID 始终为 5c:02:14:71:ab:37，USB 全部 active，信号 -71~-63 dBm，rx_dropped 320→331，rx/tx errors 均为 0。
- 本机 journal 未见与长事件对应的 disconnect/deauth/route 变化；电台当前日志也未见对应 disconnect/deauth/suspend/resume。单凭 rx_dropped 计数不能定位丢包层级。
- 第四轮前的只读日志核查：第二次长事件对应的电台 uptime 3135-3145 秒窗口没有 scan/cfg80211/rtl/8723/deauth/disassoc 内核记录；不能仅凭“无日志”排除扫描。
- NetworkManager 启动日志明确配置 `bgscan='simple:30:-80:86400'`；当前信号约 -63 dBm，按该阈值应使用长间隔，但仍需直接观测实际 Scanning 状态。
- D-Bus 只读查询确认 `/fi/w1/wpa_supplicant1/Interfaces/0` 对应 wlan0、State=completed，基线 Scanning=false；第四轮以约 100 ms 轮询该属性并同时采集 `iw event -t`，不触发主动扫描。
- `iw dev` 确认 wlan0 与 disconnected wlan1 同属 phy#0，因此任一接口的 off-channel scan 都可能影响 wlan0；第四轮的 `iw event` 覆盖整个 phy，后续 Scanning 分析需同时考虑 Interfaces/0 与 Interfaces/1。
- 第四轮采集进行中（runId=pre-fix）：在主采集之外，单独启动 `iw event -t wlan1` 专用采集（证据目录 iw_event_wlan1.log，Debug Server location=radio.iw.event.wlan1），以便明确事件归属到 wlan1，避免与 wlan0 全局事件混淆。
- 第四轮主采集复现阻塞：本机→电台 300/300、max_rtt=10055 ms、14 次超时；电台→本机 300/300、max_rtt=10077.515 ms；网关 300/300、max_rtt=260 ms、0 超时。radio_sampler 300 次采样 max_remote_epoch_gap_s=2，但本机 max_receive_gap_s=19.083；rx_dropped 395→448，USB 仍全部 active，BSSID 不变，信号 -66~-62 dBm。再次确认无线会话双向数据面停顿后集中释放。
- 采集器缺陷定位：电台为 BusyBox，没有 `timeout` 命令，且该版 `iw event` 不接受接口名参数（只能全局监听 phy）。因此第四轮的 scan_monitor 和 iw event 监视器均以 returncode=127 退出、0 样本，未采到扫描证据（已记录在 NDJSON）。
- 修复：所有远端长命令改为用本机侧 `timeout` 约束 SSH 进程；`iw event -t` 改为全局采集并从行内容标记 wlan0/wlan1；Scanning 同时轮询 `/fi/w1/wpa_supplicant1/Interfaces/0`（wlan0）和 `Interfaces/1`（wlan1）。第五轮已用修复后的 capture_1hz.py 启动干净同步采集。
- 第五轮新证据：`iw event -t` 明确捕获到断开的 wlan1 在共享 phy#0 上周期性发起全频段扫描，观察到两次：kernel_ts=1795225699.555589→.555975（间隔约 0.4 ms）和 1795225762.550560→.550890，间隔约 63 秒；两次均 `scan finished: 2412…2472`。wlan0 未出现 scan 事件。
- 由于这两次 wlan1 扫描的 iw event 起止间隔仅 ~0.4 ms，且 100 ms 轮询的 D-Bus `Scanning` 未观测到 true，初步判断这两次扫描未占用真实空中时间（可能返回缓存/空结果），暂不能直接解释 10 秒阻塞；需等待第五轮出现大阻塞后做时间对齐，必要时主动触发 wlan1 扫描做对照。
- 第五轮干净时间线（runId=pre-fix，起点 epoch=1795225671.290）：wlan1 扫描严格每 63 s 一次（kernel_ts 699.556 / 762.551 / 825.556 / 888.555 / 951.553），均 ~0.4 ms 完成；本轮未复现 10 s 阻塞（本地 max_rtt=1270 ms、反向 1551 ms，丢包 4%/3%）。本轮最大 SSH 送达间隙 6.954 s（本地 886.877→893.831）把 888.555 的扫描包在其中，附近有零散 GAP（seq 218/220/221/226），但其余 4 次扫描附近无明显停顿，相关性不足。
- 主动触发扫描对照实验（runId=exp-scan-trigger）：在电台侧第 10 秒执行 `iw dev wlan1 scan`，同时本机 0.2 s 快 ping。结果：扫描 kernel started=1795226919.592808→finished=.594546（约 1.7 ms），扫描时刻本地 ping 无任何停顿（随后 seq64 rtt=108 ms）；扫描结果文件 827 行含真实 BSS（wifi-8），证明是真实扫描。**【⚠️ 已修正——此结论不成立】** ~~wlan1 扫描（含周期性的 63 s 扫描）不会导致 wlan0 数据面停顿，扫描假设 REJECTED。~~ 该主动实验返回的是缓存结果（kernel 1.7 ms、`Scanning` 从未变 true、未真正离信道），因此未扰动 wlan0，不能否定真实周期性离信道扫描的影响。决定性证据见下方"突破"小节。（反向 ping 因 BusyBox 不支持 `-O`/小数 `-i` 立即退出，与结论无关。）
- 剩余根因范围：rtl8723du 驱动/固件或 USB 数据面的间歇性静默停顿（staging 驱动），或 AP 对该客户端的队列/交付异常。注意到电台负载偏高（load avg 3.74/4.15），需排查 CPU 饥饿是否参与。下一步：在下一次 10 s 阻塞窗口内，用本机侧定时拉取的电台 `dmesg`/统计快照捕获驱动级证据。
- **活抓阻塞（第六轮，本地证据）**：本机观测到电台 ARP=INCOMPLETE、ping 立即 `Destination Host Unreachable`、5/5 全丢（约 10:16:33–10:16:37），而网关 5/5 全通（8–25 ms）；约 10:17:32 电台恢复（首次 ping rtt=21 ms）。时间锚点：恢复瞬间 host_epoch=1795227452.7 ↔ radio_uptime=6025.7，故主机观测到不可达时 radio_uptime≈5966，结合 ARP 过期推算阻塞始于 radio_uptime≈5906 附近。
- **电台侧本地快照（stall_watch.sh，每 2s，不依赖 WiFi）覆盖 uptime 5808–6025，含上述阻塞窗口**：全程 `dmesg_lines` 恒为 310（rtl8723du staging 驱动从零日志，阻塞/恢复均无 disconnect/deauth/USB reset/错误）；链路始终 `Connected`，BSSID `5c:02:14:71:ab:37` 不变（无重关联）；信号 -62~-71 dBm（uptime 6004 处短暂下探 -71 后恢复，非持续信号丢失）；wlan0 rx/tx 计数器每 2s 递增（drx 1–9、dtx 0–11），无 ≥4s 的连续冻结；仅 uptime 5907–5920 附近零星 rx_dropped（+3/+2/+1）。`iw dev wlan0 station dump` 在该电台无输出（此 iw 构建不支持），故无 tx retries/failed 数据。
- **上述证据的含义**：阻塞期间电台 wlan0 仍在与 AP 交换帧（计数器递增）、链路不掉、驱动零日志 → 不支持“驱动/PHY 完全冻结”子假设，更偏向 AP 对该客户端的单播交付异常（G）或固件面单播静默停顿（F 的子情形）。但本轮缺主机侧连续 ping 与电台 uptime 的精确对齐，需以下一轮两侧同步采集确认“阻塞瞬间计数器是否冻结”作为判据。
- **AP 侧探活**：192.168.31.1 无 SSH（22 端口拒绝），HTTP/HTTPS 返回 nginx/1.12.2 登录页（小米路由）；网关 MAC `5c:02:14:71:ab:35` 与 AP BSSID `5c:02:14:71:ab:37` 同 OUI，确为同一设备。取 AP 侧客户端日志/队列需管理员凭据（用户门控），暂不具备。
- **采集器运维记录**：恢复监视器的 `pgrep` 在非交互 SSH shell 中因 PATH 问题返回 `not found`，误判 watcher 已死而重复 relaunch，导致 `/tmp/stall_watch.log` 被两个 watcher 进程交错写入（出现负增量）；已改用独立日志文件 `/tmp/stall_watch2.log` 的单一流 watcher，并以 `/tmp/stall_watch_captured.log`（单流、uptime 5808–6023）作为阻塞窗口的干净证据。后续远端长循环一律自计数 + 独立日志。
- **修复候选 F1（驱动智能省电，待证据确认后 A/B 验证）**：`/sys/module/8723du/parameters/` 中省电相关参数当前值为 `rtw_power_mgnt=0`、`rtw_ips_mode=0`、`rtw_enusbss=0`、`rtw_low_power=0`、`rtw_lps_level=0`，但 `rtw_smart_ps=2`、`rtw_lps_chk_by_tp=1` 非零——此前基线仅核查了前者，遗漏了驱动内部智能省电。`rtw_smart_ps`/`rtw_lps_chk_by_tp` 在 sysfs 中均为 `-rw-r--r--`（root 运行时可写，无需重载模块、不中断连接），是首选最小变更候选：置 `rtw_smart_ps=0` 后用相同 1Hz 双向采集做 pre/post 对照。注意：mac80211 级 Power save=off 不等于驱动固件级 smart_ps 关闭，二者独立。
- **突破：600 s 两侧对齐采集（runId=pre-fix，CAPTURE_START host_epoch=1795227837.114 ↔ radio_uptime=6410.12）完成并证据闭环**（分析器 `/tmp/analyze_reverse_stall.py`）：
  - 反向 ping（电台→本机）600/600 回复、0% 丢包，但 **max_rtt=9333 ms**；共检出 **10 次阻塞突发**，首 seq 依次为 `[4, 67, 130, 193, 256, 319, 382, 445, 508, 571]`，**相邻突发间距全部精确等于 63 s**；每次突发呈典型队列排空签名（首包 RTT 8226–9333 ms，随后逐包递减约 1000 ms）。
  - 同期 watcher2（每 2 s 快照，353 条，radio_uptime 6392.8–7148.8）显示：**每一次**阻塞突发都呈固定形态——先 drain 突增（如 drx=+19/dtx=+21、drx=+35/dtx=+40），**紧接着连续 2 拍以上 drx=0 且 dtx=0（约 4 s 计数器冻结）**，然后恢复。全程信号 -65~-66 dBm、`conn=Y`、BSSID 不变、`dmesg` 增量恒为 0（staging 驱动零日志）。**wlan0 计数器在阻塞瞬间冻结 → 排除纯 AP 侧单播交付异常（G），锁定为电台侧 phy/驱动面停顿。**
  - 63 s 周期与第五轮观测到的 wlan1 周期性扫描周期完全一致，且 wlan0/wlan1 共享 phy#0（`iw dev` 确认）→ wlan1 离信道扫描冻结 phy 是首要嫌疑。
- **confirm 采集（75 s）直接在预测阻塞时刻观测到 wlan1 扫描**：`iw event -t` 捕获 `wlan1 (phy #0): scan started @host_epoch 1795228786.55` → `scan aborted @1795228790.01`（历时 **3.46 s**），列出全部 2.4 GHz 信道 2412–2472。该时刻距 burst#0（epoch 1795227841.114）为 **945.44 s = 15.007 × 63**，**精确落在 63 s 阻塞网格的第 15 号预测突发时刻**（误差 0.44 s）。这是 wlan1 离信道扫描恰好在预测阻塞时刻发生的直接观测证据。
  - 说明：confirm 期间本地 ping 因 wrapper 提前结束只记录到 seq 1–4（未覆盖扫描窗口），不能用作反证，但与上述相关性不矛盾；iwevent + 600 s 对齐采集的计数器冻结已构成独立充分证据。
- **对先前"扫描 REJECTED"结论的修正**：第五轮末的主动触发实验（`iw dev wlan1 scan`）返回的是缓存结果（kernel 1.7 ms、`Scanning` 从未变 true、未真正离信道），因此未扰动 wlan0；该实验不能否定真实周期性离信道扫描的影响。决定性证据是 63 s 周期 + 阻塞瞬间 wlan0 计数器冻结 + confirm 在预测时刻直接观测到 wlan1 扫描三者同时成立。
- **当前电台状态（只读）**：wlan0 `UP/LOWER_UP` 已连接 wifi-8（ch6）；wlan1 `UP` 但 `NO-CARRIER`/disconnected（无 NM connection），与 wlan0 同属 phy#0；`rtw_smart_ps=2`、`rtw_lps_chk_by_tp=1`（均运行时可写），`rtw_power_mgnt=0`；wpa_supplicant 由 NM 经 D-Bus 管控。
- **最小修复方案（待用户批准，未实施）**：禁用 wlan1（`ip link set wlan1 down`，并让 NetworkManager 不管理 wlan1 以防其再次周期性扫描）。wlan1 未连接、无业务用途，变更可逆。若禁用 wlan1 后阻塞仍残留，再以 F1（`echo 0 > /sys/module/8723du/parameters/rtw_smart_ps`，运行时可写、不中断连接）作为二级杠杆做 A/B 验证。

## Verification Conclusion
**根因已确认**：断开的 **wlan1 每 63 s 在共享 phy#0 上发起离信道全频段扫描**，扫描期间整个 phy 被冻结约 4–8 s，导致同 phy 的 wlan0 数据面停摆；ICMP 在此期间排队，扫描结束后集中释放，表现为约 10 秒的间歇无响应 + 恢复后 RTT 逐包递减。

证据闭环（独立三重）：
1. **周期性**：反向 ping 600 s 内 10 次阻塞突发，相邻间距全部精确为 63 s（与 wlan1 扫描周期一致），呈队列排空签名（首包 RTT 8226–9333 ms 后逐包递减约 1000 ms）。
2. **计数器冻结**：watcher2 显示每次阻塞瞬间 wlan0 rx/tx 计数器出现 ~4 s 连续零增量（phy 冻结），而信号/-65~-66、链路、BSSID、dmesg 全程正常 → 排除纯 AP 侧单播交付异常（G）。
3. **直接观测**：confirm 采集在 63 s 网格预测的阻塞时刻（误差 0.44 s）直接捕获 `wlan1 scan started`，遍历 2412–2472 全部信道、历时 3.46 s。

对已捕获的长事件，USB runtime suspend、已核查的 mac80211/NM 省电配置、IP/MAC 冲突、整机 CPU/用户空间停顿均有反证。ARP FAILED/INCOMPLETE 是阻塞期间电台不应答后的现象，非根因。BSSID 和 carrier 不变也不支持完整重连。先前"扫描 REJECTED"的结论基于一次返回缓存结果的非离信道主动扫描，已修正。驱动内部智能省电（`rtw_smart_ps=2`，F1）仍作为禁用 wlan1 后若残留阻塞的二级验证项。当前仍未修改任何电台配置，等待用户批准最小修复。

## Final Evidence Gate
- **Evidence Sources**: /tmp/x6200-civ-control-radio-ping-stalls/trae-debug-log-radio-ping-stalls.ndjson；/tmp/aligned_cap/{reverse_ping.log,watcher_logs.txt}（600 s 对齐采集）；/tmp/confirm/iwevent.log（confirm 扫描事件）；/tmp/analyze_reverse_stall.py（关联分析器）；本机 journalctl/ip/sysfs 采样；电台 SSH 只读 runtime/link/电源参数/日志
- **progress_status**: root-cause CONFIRMED；等待用户批准禁用 wlan1 的最小修复，随后做同条件 pre/post A/B 复测
- **Hypothesis Conclusions**: A=REJECTED（已捕获事件）；B=已检查配置路径不成立；C=INCONCLUSIVE（ARP 失败是阻塞结果）；D=扫描子假设 **CONFIRMED**（wlan1 每 63 s 离信道扫描冻结共享 phy#0，阻塞瞬间 wlan0 计数器 ~4 s 冻结 + confirm iw-event 在预测时刻直接观测；先前 REJECTED 基于缓存扫描已修正），整机停顿子假设 REJECTED；E=REJECTED；F=OPEN（驱动/固件/USB 静默停顿为残留可能，F1 `rtw_smart_ps` 作二级杠杆）；G=OPEN 但被削弱（阻塞瞬间 wlan0 计数器冻结，不支持纯 AP 侧单播交付异常）
- **Approved Fix (pending user approval)**: 禁用 wlan1（`ip link set wlan1 down` + `nmcli device set wlan1 managed no`），可逆；post-fix 以相同 1 Hz 双向采集 + watcher + `iw event` 验证 63 s 阻塞与计数器冻结消失、wlan1 不再扫描。

## Post-Fix Verification (A/B)
**修复已实施（用户批准）**：`nmcli device set wlan1 managed no` + `ip link set wlan1 down`。实施后即时验证：wlan1 标志位 `<BROADCAST,MULTICAST>`（无 `UP`）、NM 状态 `unmanaged`；wlan0 仍 `connected:wifi-8`，反向 ping 2/2 通——wlan0 完全不受影响。

**采集**（CAPTURE_START host_epoch=1795229769.218 ↔ radio_uptime=8342.17，时长 ~422 s，watcher 覆盖 uptime 8342.7–8765.2，约 6–7 个原本会触发阻塞的 63 s 窗口）：本机 `stdbuf -oL ping -D -i 1 -c 420` 电台/网关 + 反向 `ping -i 1 -c 420` + 每 2 s watcher + 全局 `iw event`。分析器 `/tmp/analyze_postfix.py`。

| 指标 | PRE-FIX | POST-FIX |
|------|---------|----------|
| 本机→电台 max RTT | 10055 ms | **150 ms**（420 发 / 419 收，丢包 0.24%） |
| 电台→本机 max RTT | 9333 ms | **490 ms**（300 s 采集 SSH 被远端关闭，采到 seq 0–278 / 259 回复，0 突发） |
| 阻塞突发数（连续 RTT>1000 ms） | 10 | **0** |
| 突发周期 | 63 s | n/a |
| wlan0 计数器冻结窗口（≥2 拍 drx=0 & dtx=0） | 10 | **0**（rx/tx 每拍均递增，min delta 268/252 字节） |
| wlan1 扫描事件（iw event） | 每 63 s 一次 | **0**（422 s 全程 + 后续 30 s 复核均无） |
| wlan0 链路掉线拍数 / dmesg 增量 | 0 / 0 | 0 / 0 |

**结论**：禁用 wlan1 后，63 s 周期性阻塞、wlan0 计数器冻结、wlan1 离信道扫描三者**全部消失**，max RTT 从 ~10 s 降至 ~150 ms。根因（wlan1 周期性扫描冻结共享 phy#0）与修复均得到验证。注意：watcher 的 `sig` 字段因 `/proc/net/wireless` 列解析偏差读出了链路质量值（82–98）而非信号电平，但 `link_down_snaps=0` 与 `dmesg_delta=0` 仍确认链路全程稳定，不影响冻结/扫描结论。已补跑 300 s 干净反向采集：max 490 ms、0 突发，双向（本机→电台、电台→本机）对照均确认修复。

## 持久化（已实施）
运行时修复（`ip link set wlan1 down` + `nmcli device set wlan1 managed no`）在电台重启后会失效，故追加持久化配置，保证重启后 wlan1 仍不被 NM/wpa_supplicant 接管扫描。

- 系统：Buildroot 2020.02.9 + BusyBox init（SysV 脚本 `/etc/init.d/S45network-manager` 启动 NM）；根文件系统为 `/dev/mmcblk1p2` 上的 **ext4（rw）**，写入持久、重启不丢失（已验证可写）。
- 变更：新建 `/etc/NetworkManager/conf.d/99-unmanaged-wlan1.conf`（最小、可逆，未改动任何现有文件）：
  ```
  [keyfile]
  unmanaged-devices=interface-name:wlan1
  ```
- 生效验证：`nmcli connection reload`（该版本 NM 不支持 `nmcli general reload`）后，`nmcli device` 显示 `wlan1:wifi:unmanaged`、wlan1 无 `UP` 标志；`wlan0` 仍 `connected:wifi-8` / `<UP,LOWER_UP>`，SSH 未中断——wlan0 数据面不受影响。
- 为何足够：wlan1 的周期性扫描由 NM 经 D-Bus 驱动 wpa_supplicant 发起；NM 不管理 wlan1 后，wpa_supplicant 不再为其启动扫描。`/etc/network/interfaces` 仅含 lo、`/etc` 全量 grep 无 wlan1 引用，故无其他机制会在开机时拉起 wlan1。
- 回滚：删除 `/etc/NetworkManager/conf.d/99-unmanaged-wlan1.conf` 后 `nmcli connection reload`（或重启）即恢复 NM 对 wlan1 的管理与扫描。
- 待确认：建议电台下次重启后复核 `nmcli device | grep wlan1` 仍为 `unmanaged` 且无 63 s 阻塞，作为持久化的最终验收。
