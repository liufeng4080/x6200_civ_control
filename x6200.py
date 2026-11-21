#!/usr/bin/env python3
"""X6200 WFSERVER U-DIG 网络 CW 发射客户端。"""

import argparse
from collections import deque
import errno
import logging
import math
import os
import socket
import struct
import sys
import time


CONTROL_PORT = 50001
CONTROLLER_CIV = 0xE1
DEFAULT_RADIO_CIV = 0xA4
CLIENT_NAME = b"auto-x6200"
RX_CODEC = 0x04
TX_CODEC = 0x04
RX_SAMPLE_RATE = 8000
SAMPLE_RATE = 48000
TX_LATENCY_MS = 800
AUDIO_HEADER_SIZE = 24
AUDIO_SAMPLES_PER_TICK = 600
AUDIO_PCM_BYTES_PER_TICK = AUDIO_SAMPLES_PER_TICK * 2
# 与 wfview 一致：每个数据报 600 样本 / 1200 字节，远低于 MTU。
AUDIO_MAX_DATAGRAM_PCM = 1200
AUDIO_TICK_PERIOD_NS = 12_500_000
# 提前 2 个调度槽投递，吸收本机线程调度抖动；电台侧由 TX_LATENCY_MS 缓冲。
AUDIO_SEND_AHEAD_NS = 2 * AUDIO_TICK_PERIOD_NS
PREROLL_SAMPLES = SAMPLE_RATE
TAIL_SAMPLES = SAMPLE_RATE
MAX_AUDIO_LATENESS_NS = 100_000_000
FT8_SLOT_PERIOD_NS = 15_000_000_000
# 时隙选择余量：握手后至少留 8 秒，用于静音预填充与 PTT 两次尝试。
FT8_START_LEAD_NS = 8_000_000_000
# PTT 请求提前量：CI-V ACK 与 TX 状态回读实测抖动 0.01~3 秒。
# 实验值 1 秒（原 4 秒）。本电台 PTT→TX 确认约需 3 秒，1 秒提前量下确认常迟到；
# 迟到是否仍发射由 PTT_LATE_TOLERANCE_NS 决定，超过容忍才安全关 PTT、不发残缺突发。
FT8_PTT_LEAD_NS = 1_000_000_000
# PTT/TX 确认迟到容忍：确认时刻晚于时隙起点不超过该值则仍发射（DT 相应偏移），超过则中止。
PTT_LATE_TOLERANCE_NS = 3_000_000_000
TRACKED_CACHE_SIZE = 500  # 与 wfview BUFSIZE 一致，可缓存 6.25 秒网络音频。
MAX_DATAGRAM = 65535

HANDSHAKE_TIMEOUT = 3.0
DISCOVERY_INTERVAL = 0.75
AUTH_TIMEOUT = 8.0
CIV_OPEN_TIMEOUT = 0.6
CIV_ACK_TIMEOUT = 2.0
CIV_READ_TIMEOUT = 6.0
CIV_PTT_QUERY_INTERVAL = 1.0
CIV_IDLE_INTERVAL = 0.1
CIV_OPEN_INTERVAL = 0.1
UDIG_SET_ATTEMPTS = 3
UDIG_SET_RETRY_DELAY = 0.3
PING_INTERVAL = 0.5
POLL_INTERVAL = 0.002

LOG = logging.getLogger("x6200")


class X6200Error(Exception):
    """协议或会话失败。"""


class ProtocolTimeout(X6200Error):
    """远端未在截止时间内给出所需应答。"""


class CIVNack(X6200Error):
    """电台返回 CI-V FA。"""


class BusyError(X6200Error):
    """电台或本机资源被其他客户端占用。"""


class PTTError(X6200Error):
    """PTT 命令或状态确认失败。"""


class AudioScheduleError(X6200Error):
    """音频发送无法维持安全节奏。"""


PASSCODE_SEQUENCE = bytes((
    0x47, 0x5D, 0x4C, 0x42, 0x66, 0x20, 0x23, 0x46,
    0x4E, 0x57, 0x45, 0x3D, 0x67, 0x76, 0x60, 0x41,
    0x62, 0x39, 0x59, 0x2D, 0x68, 0x7E, 0x7C, 0x65,
    0x7D, 0x49, 0x29, 0x72, 0x73, 0x78, 0x21, 0x6E,
    0x5A, 0x5E, 0x4A, 0x3E, 0x71, 0x2C, 0x2A, 0x54,
    0x3C, 0x3A, 0x63, 0x4F, 0x43, 0x75, 0x27, 0x79,
    0x5B, 0x35, 0x70, 0x48, 0x6B, 0x56, 0x6F, 0x34,
    0x32, 0x6C, 0x30, 0x61, 0x6D, 0x7B, 0x2F, 0x4B,
    0x64, 0x38, 0x2B, 0x2E, 0x50, 0x40, 0x3F, 0x55,
    0x33, 0x37, 0x25, 0x77, 0x24, 0x26, 0x74, 0x6A,
    0x28, 0x53, 0x4D, 0x69, 0x22, 0x5C, 0x44, 0x31,
    0x36, 0x58, 0x3B, 0x7A, 0x51, 0x5F, 0x52,
))


def encode_passcode(value):
    """生成 wfview 登录使用的 16 字节混淆字段。"""
    output = bytearray(16)
    for index, byte in enumerate(value.encode("utf-8")[:16]):
        position = byte + index
        if position > 126:
            position = 32 + position % 127
        if 32 <= position <= 126:
            output[index] = PASSCODE_SEQUENCE[position - 32]
    return bytes(output)


def endpoint_id(local_ip, local_port):
    """与 wfview/Qt 的 endpointId 字节顺序保持一致。"""
    packed = socket.inet_aton(local_ip)
    return (packed[2] << 24) | (packed[3] << 16) | (local_port & 0xFFFF)


def build_control_packet(my_id, remote_id, packet_type, seq=0):
    packet = bytearray(16)
    struct.pack_into("<IHHII", packet, 0, 16, packet_type, seq, my_id, remote_id)
    return bytes(packet)


def build_ping_packet(my_id, remote_id, seq, milliseconds):
    packet = bytearray(0x15)
    struct.pack_into("<IHHII", packet, 0, 0x15, 0x07, seq, my_id, remote_id)
    struct.pack_into("<I", packet, 0x11, milliseconds & 0xFFFFFFFF)
    return bytes(packet)


def build_auth_header(length, request_type, auth_seq, tok_request, token,
                      my_id, remote_id):
    if length < 0x20:
        raise ValueError("认证封包长度不足")
    packet = bytearray(length)
    struct.pack_into("<I", packet, 0, length)
    struct.pack_into("<II", packet, 8, my_id, remote_id)
    struct.pack_into(">I", packet, 0x10, length - 0x10)
    packet[0x14] = 1
    packet[0x15] = request_type
    struct.pack_into(">H", packet, 0x16, auth_seq & 0xFFFF)
    struct.pack_into("<H", packet, 0x1A, tok_request & 0xFFFF)
    struct.pack_into("<I", packet, 0x1C, token & 0xFFFFFFFF)
    return packet


def build_civ_frame(radio_civ, body):
    return b"\xFE\xFE" + bytes((radio_civ, CONTROLLER_CIV)) + bytes(body) + b"\xFD"


def build_civ_open_packet(my_id, remote_id, data_seq, opened=True):
    packet = bytearray(0x16)
    struct.pack_into("<I", packet, 0, len(packet))
    struct.pack_into("<II", packet, 8, my_id, remote_id)
    struct.pack_into("<H", packet, 0x10, 0x01C0)
    struct.pack_into(">H", packet, 0x13, data_seq & 0xFFFF)
    packet[0x15] = 0x04 if opened else 0x00
    return bytes(packet)


def build_civ_data_packet(frame, my_id, remote_id, data_seq):
    if len(frame) > 0xFFFF:
        raise ValueError("CI-V 帧过长")
    packet = bytearray(0x15 + len(frame))
    struct.pack_into("<I", packet, 0, len(packet))
    struct.pack_into("<II", packet, 8, my_id, remote_id)
    packet[0x10] = 0xC1
    struct.pack_into("<H", packet, 0x11, len(frame))
    struct.pack_into(">H", packet, 0x13, data_seq & 0xFFFF)
    packet[0x15:] = frame
    return bytes(packet)


def iter_civ_frames(payload):
    """从一个 UDP 载荷中提取全部完整 CI-V 帧，容忍帧前噪声。"""
    payload = bytes(payload)
    cursor = 0
    length = len(payload)
    while cursor + 6 <= length:
        start = cursor
        while start + 1 < length and payload[start:start + 2] != b"\xFE\xFE":
            start += 1
        if start + 6 > length:
            return
        end = payload.find(b"\xFD", start + 4)
        if end < 0:
            return
        yield payload[start:end + 1]
        cursor = end + 1


def parse_civ_datagram(packet):
    """校验网络 CI-V 封装并返回其中的帧列表。"""
    if len(packet) <= 0x15:
        return []
    if struct.unpack_from("<I", packet, 0)[0] != len(packet):
        raise ValueError("CI-V 数据报总长度不匹配")
    if struct.unpack_from("<H", packet, 0x11)[0] + 0x15 != len(packet):
        raise ValueError("CI-V 数据报载荷长度不匹配")
    return list(iter_civ_frames(packet[0x15:]))


def build_audio_packet(outer_seq, audio_seq, my_id, remote_id, pcm):
    """构造 24 字节网络音频头并附加 PCM16LE。"""
    pcm = bytes(pcm)
    if len(pcm) & 1:
        raise ValueError("PCM16LE 字节数必须为偶数")
    if len(pcm) > 0xFFFF:
        raise ValueError("单个音频封包过大")
    header = bytearray(AUDIO_HEADER_SIZE)
    struct.pack_into("<IHHII", header, 0,
                     AUDIO_HEADER_SIZE + len(pcm), 0, outer_seq & 0xFFFF,
                     my_id, remote_id)
    struct.pack_into("<H", header, 0x10, 0x0080)
    struct.pack_into(">H", header, 0x12, audio_seq & 0xFFFF)
    struct.pack_into("<H", header, 0x14, 0)
    struct.pack_into(">H", header, 0x16, len(pcm))
    return bytes(header) + pcm


def _fixed_string(data):
    return bytes(data).split(b"\0", 1)[0].decode("utf-8", "replace")


def find_wfview_processes(proc_root="/proc"):
    """只读检查本机进程，不发送信号。"""
    found = []
    try:
        entries = os.scandir(proc_root)
    except OSError:
        return found
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            comm_path = os.path.join(entry.path, "comm")
            try:
                with open(comm_path, "r", encoding="utf-8", errors="replace") as stream:
                    name = stream.read().strip()
            except OSError:
                continue
            normalized = os.path.basename(name).lower()
            if normalized == "wfview" or normalized.startswith("wfview-"):
                found.append((int(entry.name), name))
    return found


def ensure_no_wfview():
    conflicts = find_wfview_processes()
    if conflicts:
        details = ", ".join("PID {} ({})".format(pid, name) for pid, name in conflicts)
        raise BusyError("检测到 wfview 进程：{}；请关闭后重试".format(details))


class UDPLink:
    """未 connect 的 UDP 链接；socket、时钟和睡眠函数均可替换。"""

    def __init__(self, name, host, remote_port, sock, local_ip,
                 clock=time.monotonic_ns, sleeper=time.sleep):
        self.name = name
        self.host = host
        self.remote_port = remote_port
        self.sock = sock
        self.local_ip = local_ip
        self.local_port = sock.getsockname()[1]
        self.my_id = endpoint_id(local_ip, self.local_port)
        self.remote_id = 0
        self.send_seq = 0
        self.data_seq = 0
        self.ping_seq = 0
        self.remote_closed = False
        self.rx_datagrams = 0
        self.tx_cache = deque(maxlen=TRACKED_CACHE_SIZE)
        self.clock = clock
        self.sleeper = sleeper

    def send_raw(self, packet, remote_port=None):
        port = self.remote_port if remote_port is None else remote_port
        if not port:
            raise X6200Error("{} 远端端口尚未协商".format(self.name))
        sent = self.sock.sendto(bytes(packet), (self.host, port))
        if sent != len(packet):
            raise OSError("短 UDP 发送：{}/{} 字节".format(sent, len(packet)))

    def send_tracked(self, packet):
        packet = bytearray(packet)
        if len(packet) < 8:
            raise ValueError("tracked 封包长度不足")
        seq = self.send_seq
        struct.pack_into("<H", packet, 6, seq)
        cached = bytes(packet)
        self.tx_cache.append((seq, cached))
        self.send_seq = (self.send_seq + 1) & 0xFFFF
        self.send_raw(cached)

    def retransmit(self, seq):
        for cached_seq, packet in self.tx_cache:
            if cached_seq == seq:
                LOG.warning("%s 按电台请求重传 seq=%d", self.name, seq)
                self.send_raw(packet)
                return True
        LOG.warning("%s 电台请求重传 seq=%d，但最近 %d 个缓存中不存在",
                    self.name, seq, self.tx_cache.maxlen)
        return False

    def send_control(self, packet_type, seq=0):
        self.send_raw(build_control_packet(
            self.my_id, self.remote_id, packet_type, seq))

    def send_ping(self):
        packet = build_ping_packet(
            self.my_id, self.remote_id, self.ping_seq,
            self.clock() // 1_000_000)
        self.ping_seq = (self.ping_seq + 1) & 0xFFFF
        self.send_raw(packet)

    def recv(self):
        try:
            packet, source = self.sock.recvfrom(MAX_DATAGRAM)
        except (BlockingIOError, InterruptedError, socket.timeout):
            return None
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                return None
            raise
        self.rx_datagrams += 1
        if source[0] != self.host:
            LOG.warning("%s 忽略非电台来源 %s", self.name, source[0])
            return None
        return packet

    def housekeeping(self, packet):
        if len(packet) < 16:
            return True
        packet_type = struct.unpack_from("<H", packet, 4)[0]
        if packet_type == 0x01:
            if len(packet) == 16:
                self.retransmit(struct.unpack_from("<H", packet, 6)[0])
            else:
                for offset in range(16, len(packet) - 1, 2):
                    self.retransmit(struct.unpack_from("<H", packet, offset)[0])
            return True
        if len(packet) == 0x15 and packet_type == 0x07:
            if packet[16] == 0:
                reply = bytearray(packet)
                struct.pack_into("<II", reply, 8, self.my_id, self.remote_id)
                reply[16] = 1
                self.send_raw(reply)
            return True
        if len(packet) == 16 and packet_type == 0x05:
            self.remote_closed = True
            LOG.warning("%s 收到电台 disconnect", self.name)
            return True
        return False

    def handshake(self, timeout=HANDSHAKE_TIMEOUT):
        deadline = self.clock() + int(timeout * 1_000_000_000)
        next_discovery = self.clock()
        ready_sent = False
        while self.clock() < deadline:
            now = self.clock()
            if not ready_sent and now >= next_discovery:
                self.send_control(0x03, 0)
                next_discovery = now + int(DISCOVERY_INTERVAL * 1_000_000_000)
            packet = self.recv()
            if packet is None:
                self.sleeper(POLL_INTERVAL)
                continue
            if self.housekeeping(packet):
                if self.remote_closed:
                    raise X6200Error("{} 握手期间被电台断开".format(self.name))
                continue
            if len(packet) != 16:
                continue
            packet_type = struct.unpack_from("<H", packet, 4)[0]
            if packet_type == 0x04:
                self.remote_id = struct.unpack_from("<I", packet, 8)[0]
                if not ready_sent:
                    self.send_control(0x06, 1)
                    ready_sent = True
            elif packet_type == 0x06 and ready_sent:
                self.remote_id = struct.unpack_from("<I", packet, 8)[0]
                LOG.info("%s AYT/Here/Ready 握手完成", self.name)
                return
        raise ProtocolTimeout("{} AYT/Here/Ready 握手超时".format(self.name))

    def close(self):
        self.sock.close()


class X6200Session:
    """WFSERVER 登录、CI-V、U-DIG 与网络音频会话。"""

    def __init__(self, control, civ, audio, user="user", password="123",
                 clock=time.monotonic_ns, sleeper=time.sleep):
        self.control = control
        self.civ = civ
        self.audio = audio
        self.user = user
        self.password = password
        self.clock = clock
        self.sleeper = sleeper
        self.auth_seq = 0x30
        self.tok_request = 0
        self.token = 0
        self.authenticated = False
        self.token_ok = False
        self.caps_received = False
        self.conninfo_ready = False
        self.stream_sent = False
        self.radio_identity = bytes(16)
        self.radio_name = ""
        self.radio_civ = DEFAULT_RADIO_CIV
        self.remote_civ_port = 0
        self.remote_audio_port = 0
        self.civ_opened = False
        self.civ_data_seen = False
        self.audio_opened = False
        self.udig_verified = False
        self.last_ptt_state = None
        self.tx_monitor = False
        self.next_tx_query_ns = 0
        self.rx_audio_packets = 0
        self.next_civ_open = 0
        self.next_civ_idle = 0
        self.next_civ_ping = 0
        self.next_audio_ping = 0
        self.next_control_ping = 0
        self.closed = False

    @staticmethod
    def _route_local_ip(host):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((host, CONTROL_PORT))
            return probe.getsockname()[0]
        finally:
            probe.close()

    @classmethod
    def create(cls, host, user, password, clock=time.monotonic_ns,
               sleeper=time.sleep):
        host = socket.gethostbyname(host)
        local_ip = cls._route_local_ip(host)
        links = []
        try:
            for name, remote_port, bind_port in (
                    ("control", CONTROL_PORT, CONTROL_PORT),
                    ("CI-V", 0, 0), ("audio", 0, 0)):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sock.bind(("0.0.0.0", bind_port))
                    sock.setblocking(False)
                except BaseException:
                    sock.close()
                    raise
                try:
                    link = UDPLink(name, host, remote_port, sock, local_ip,
                                   clock=clock, sleeper=sleeper)
                except BaseException:
                    sock.close()
                    raise
                links.append(link)
        except BaseException as exc:
            for link in links:
                link.close()
            if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE:
                raise BusyError(
                    "本机 UDP 50001 绑定冲突；请关闭 wfview 或其他占用进程") from exc
            raise
        return cls(links[0], links[1], links[2], user, password,
                   clock=clock, sleeper=sleeper)

    def _next_auth_seq(self):
        value = self.auth_seq
        self.auth_seq = (self.auth_seq + 1) & 0xFFFF
        return value

    def _auth_header(self, length, request_type, token=None):
        return build_auth_header(
            length, request_type, self._next_auth_seq(), self.tok_request,
            self.token if token is None else token,
            self.control.my_id, self.control.remote_id)

    def send_login(self):
        self.tok_request = ((self.clock() // 1_000_000) ^
                            self.control.my_id ^ 0xA55A) & 0xFFFF
        if self.tok_request == 0:
            self.tok_request = 1
        packet = self._auth_header(0x80, 0x00, token=0)
        packet[0x40:0x50] = encode_passcode(self.user)
        packet[0x50:0x60] = encode_passcode(self.password)
        packet[0x60:0x60 + len(CLIENT_NAME)] = CLIENT_NAME
        self.control.send_tracked(packet)
        LOG.info("已发送 WFSERVER 登录请求（user=%s）", self.user)

    def send_token(self, request_type):
        packet = self._auth_header(0x40, request_type)
        struct.pack_into(">H", packet, 0x24, 0x0798)
        self.control.send_tracked(packet)

    def send_stream_request(self):
        packet = self._auth_header(0x90, 0x03)
        packet[0x20:0x30] = self.radio_identity
        name = self.radio_name.encode("utf-8", "replace")[:32]
        packet[0x40:0x40 + len(name)] = name
        packet[0x60:0x70] = encode_passcode(self.user)
        packet[0x70] = 1
        packet[0x71] = 1
        packet[0x72] = RX_CODEC
        packet[0x73] = TX_CODEC
        struct.pack_into(">I", packet, 0x74, RX_SAMPLE_RATE)
        struct.pack_into(">I", packet, 0x78, SAMPLE_RATE)
        struct.pack_into(">I", packet, 0x7C, self.civ.local_port)
        struct.pack_into(">I", packet, 0x80, self.audio.local_port)
        struct.pack_into(">I", packet, 0x84, TX_LATENCY_MS)
        packet[0x88] = 1
        self.control.send_tracked(packet)
        self.stream_sent = True
        LOG.info("已请求 CI-V/audio stream（codec=04/04，TX=48000 Hz）")

    def try_stream_request(self):
        if (not self.stream_sent and self.caps_received and
                (self.token_ok or self.conninfo_ready)):
            self.send_stream_request()

    def parse_capabilities(self, packet):
        if self.caps_received:
            return
        if len(packet) < 0xA8 or (len(packet) - 0x42) % 0x66:
            raise X6200Error("capabilities 封包长度无效")
        if (len(packet) - 0x42) // 0x66 == 0:
            raise X6200Error("capabilities 不含电台")
        radio = packet[0x42:0x42 + 0x66]
        self.radio_identity = bytes(radio[:16])
        self.radio_name = _fixed_string(radio[0x10:0x30])
        self.radio_civ = radio[0x52] or DEFAULT_RADIO_CIV
        if not self.radio_name:
            raise X6200Error("capabilities 缺少电台名称")
        self.caps_received = True
        LOG.info("capabilities：%s，CI-V=0x%02X", self.radio_name, self.radio_civ)

    def control_negotiate(self):
        self.send_login()
        deadline = self.clock() + int(AUTH_TIMEOUT * 1_000_000_000)
        while self.clock() < deadline:
            packet = self.control.recv()
            if packet is None:
                self.sleeper(POLL_INTERVAL)
                continue
            if self.control.housekeeping(packet):
                if self.control.remote_closed:
                    raise X6200Error("登录协商期间被电台断开")
                continue
            length = len(packet)
            if length == 0x60:
                if self.authenticated:
                    continue
                error = struct.unpack_from("<I", packet, 0x30)[0]
                connection = _fixed_string(packet[0x40:0x50])
                if error:
                    raise BusyError("电台拒绝登录：error=0x{:08X}，conn={}".format(
                        error, connection))
                if struct.unpack_from("<H", packet, 0x1A)[0] != self.tok_request:
                    raise X6200Error("登录响应 tok_request 不匹配")
                self.token = struct.unpack_from("<I", packet, 0x1C)[0]
                self.authenticated = True
                LOG.info("WFSERVER 登录成功，tok_request 已匹配")
                self.send_token(0x02)
            elif length == 0x40:
                if packet[0x15] != 0x05 or packet[0x14] != 0x02:
                    continue
                response = struct.unpack_from("<I", packet, 0x30)[0]
                if response:
                    raise X6200Error("token 请求被拒绝：0x{:08X}".format(response))
                self.token_ok = True
                LOG.info("WFSERVER token 校验成功")
                self.try_stream_request()
            elif length == 0x90:
                busy = struct.unpack_from("<I", packet, 0x60)[0]
                computer = _fixed_string(packet[0x64:0x74])
                if busy and computer and computer != CLIENT_NAME.decode("ascii"):
                    raise BusyError("电台正被其他客户端占用：{}".format(computer))
                self.conninfo_ready = True
                LOG.info("conninfo 可用（busy=%d，computer=%s）", busy,
                         computer or "(empty)")
                self.try_stream_request()
            elif length == 0x50:
                if not self.stream_sent:
                    continue
                error = struct.unpack_from("<I", packet, 0x30)[0]
                disconnected = packet[0x40]
                civ_port = struct.unpack_from(">H", packet, 0x42)[0]
                audio_port = struct.unpack_from(">H", packet, 0x46)[0]
                if error or disconnected == 1 or not civ_port or not audio_port:
                    raise X6200Error(
                        "stream 协商失败：error=0x{:08X}，disc={}，CI-V={}，audio={}".format(
                            error, disconnected, civ_port, audio_port))
                self.remote_civ_port = civ_port
                self.remote_audio_port = audio_port
                LOG.info("stream 就绪：CI-V=%d，audio=%d", civ_port, audio_port)
                return
            elif length >= 0xA8 and (length - 0x42) % 0x66 == 0:
                self.parse_capabilities(packet)
                self.try_stream_request()
        raise ProtocolTimeout(
            "WFSERVER 协商超时：login={} token={} caps={} conninfo={} stream={}".format(
                self.authenticated, self.token_ok, self.caps_received,
                self.conninfo_ready, self.stream_sent))

    def send_civ_open(self, opened=True):
        packet = build_civ_open_packet(
            self.civ.my_id, self.civ.remote_id, self.civ.data_seq, opened)
        self.civ.data_seq = (self.civ.data_seq + 1) & 0xFFFF
        self.civ.send_tracked(packet)

    def send_civ_idle(self):
        self.civ.send_tracked(build_control_packet(
            self.civ.my_id, self.civ.remote_id, 0, 0))

    def send_civ_frame(self, frame):
        packet = build_civ_data_packet(
            frame, self.civ.my_id, self.civ.remote_id, self.civ.data_seq)
        self.civ.data_seq = (self.civ.data_seq + 1) & 0xFFFF
        self.civ.send_tracked(packet)

    def _recv_civ_frames(self):
        packet = self.civ.recv()
        if packet is None:
            return None
        if self.civ.housekeeping(packet):
            if self.civ.remote_closed:
                raise X6200Error("CI-V 通道被电台断开")
            return []
        try:
            frames = parse_civ_datagram(packet)
        except ValueError:
            return []
        self.civ_data_seen = True
        for frame in frames:
            if (len(frame) >= 7 and frame[2] == CONTROLLER_CIV and
                    frame[3] == self.radio_civ and
                    frame[4:6] == b"\x1C\x00"):
                self.last_ptt_state = frame[6]
                LOG.info("CI-V PTT 状态回读：%s",
                         "TX" if frame[6] else "RX")
        return frames

    def drain_civ(self):
        for _ in range(64):
            if self._recv_civ_frames() is None:
                return

    def drain_control(self):
        for _ in range(16):
            packet = self.control.recv()
            if packet is None:
                return
            if self.control.housekeeping(packet) and self.control.remote_closed:
                raise X6200Error("控制通道被电台断开")

    def drain_audio(self):
        if not self.audio_opened:
            return
        for _ in range(32):
            packet = self.audio.recv()
            if packet is None:
                return
            if self.audio.housekeeping(packet):
                if self.audio.remote_closed:
                    raise X6200Error("音频通道被电台断开")
                continue
            if (len(packet) >= AUDIO_HEADER_SIZE and
                    struct.unpack_from("<H", packet, 4)[0] == 0 and
                    struct.unpack_from("<H", packet, 0x10)[0] == 0x0080):
                self.rx_audio_packets += 1

    def keepalive(self):
        now = self.clock()
        if self.control.remote_id and now >= self.next_control_ping:
            self.control.send_ping()
            self.next_control_ping = now + int(PING_INTERVAL * 1_000_000_000)
        if self.civ.remote_id:
            if self.civ_opened and not self.civ_data_seen and now >= self.next_civ_open:
                self.send_civ_open()
                self.next_civ_open = now + int(CIV_OPEN_INTERVAL * 1_000_000_000)
            if now >= self.next_civ_idle:
                self.send_civ_idle()
                self.next_civ_idle = now + int(CIV_IDLE_INTERVAL * 1_000_000_000)
            if now >= self.next_civ_ping:
                self.civ.send_ping()
                self.next_civ_ping = now + int(PING_INTERVAL * 1_000_000_000)
        if self.audio_opened and now >= self.next_audio_ping:
            self.audio.send_ping()
            self.next_audio_ping = now + int(PING_INTERVAL * 1_000_000_000)

    def service_background(self, drain_civ=False):
        self.keepalive()
        self.drain_control()
        self.drain_audio()
        if self.tx_monitor:
            now = self.clock()
            if now >= self.next_tx_query_ns:
                self.send_civ_frame(build_civ_frame(self.radio_civ, b"\x1C\x00"))
                self.next_tx_query_ns = now + int(
                    CIV_PTT_QUERY_INTERVAL * 1_000_000_000)
                drain_civ = True
        if drain_civ:
            self.drain_civ()

    def open_civ_channel(self):
        self.civ_opened = True
        self.civ_data_seen = False
        now = self.clock()
        self.next_civ_open = now
        self.next_civ_idle = now
        self.next_civ_ping = now
        self.next_control_ping = now
        deadline = now + int(CIV_OPEN_TIMEOUT * 1_000_000_000)
        while self.clock() < deadline:
            self.service_background(drain_civ=False)
            result = self._recv_civ_frames()
            if result is not None and self.civ_data_seen:
                LOG.info("CI-V open 后已收到电台数据")
                return True
            self.sleeper(POLL_INTERVAL)
        LOG.warning("CI-V open 暂未收到主动数据，将以 U-DIG 回读验证通道")
        return False

    def _wait_for_civ(self, predicate, timeout, wait_callback=None):
        deadline = self.clock() + int(timeout * 1_000_000_000)
        while self.clock() < deadline:
            self.service_background(drain_civ=False)
            if wait_callback is not None:
                wait_callback()
            frames = self._recv_civ_frames()
            if frames:
                for frame in frames:
                    if predicate(frame):
                        return frame
            self.sleeper(POLL_INTERVAL)
        raise ProtocolTimeout("CI-V 应答超时")

    def _is_ack(self, frame):
        return (len(frame) == 6 and frame[2] == CONTROLLER_CIV and
                frame[3] == self.radio_civ and frame[4] in (0xFB, 0xFA))

    def wait_ack(self, timeout=CIV_ACK_TIMEOUT, wait_callback=None):
        frame = self._wait_for_civ(self._is_ack, timeout, wait_callback)
        if frame[4] == 0xFA:
            raise CIVNack("电台返回 CI-V FA NACK")
        return frame

    def set_udig(self):
        self.drain_civ()
        self.send_civ_frame(build_civ_frame(
            self.radio_civ, b"\x26\x00\x01\x01\x01"))
        try:
            self.wait_ack()
            LOG.info("U-DIG 设置收到 FB ACK")
        except (ProtocolTimeout, CIVNack) as exc:
            LOG.warning("U-DIG 设置 ACK 未确认（%s），继续强制回读", exc)

        def is_mode(frame):
            return (len(frame) >= 10 and frame[2] == CONTROLLER_CIV and
                    frame[3] == self.radio_civ and
                    frame[4:6] == b"\x26\x00")

        # 刚打开的 CI-V 通道偶尔会丢掉第一帧模式设置，回读仍是旧值。
        # 固定重试即可，不改变“未确认 01/01/01 就禁止 PTT”的闭锁。
        mode = data_mode = filter_mode = None
        for attempt in range(UDIG_SET_ATTEMPTS):
            if attempt:
                LOG.warning("U-DIG 回读为 {:02X}/{:02X}/{:02X}，重试设置".format(
                    mode, data_mode, filter_mode))
                self.sleeper(UDIG_SET_RETRY_DELAY)
                self.drain_civ()
                self.send_civ_frame(build_civ_frame(
                    self.radio_civ, b"\x26\x00\x01\x01\x01"))
                try:
                    self.wait_ack()
                except (ProtocolTimeout, CIVNack) as exc:
                    LOG.warning("U-DIG 重试 ACK 未确认（%s），继续回读", exc)

            self.drain_civ()
            self.send_civ_frame(build_civ_frame(self.radio_civ, b"\x26\x00"))

            try:
                frame = self._wait_for_civ(is_mode, CIV_READ_TIMEOUT)
            except ProtocolTimeout as exc:
                self.udig_verified = False
                raise PTTError("U-DIG 回读超时，禁止 PTT") from exc
            mode, data_mode, filter_mode = frame[6:9]
            self.udig_verified = (mode, data_mode, filter_mode) == (1, 1, 1)
            if self.udig_verified:
                LOG.info("U-DIG 回读确认：mode=01 data=01 filter=01")
                return
        raise PTTError(
            "U-DIG 回读为 {:02X}/{:02X}/{:02X}，不是 01/01/01，禁止 PTT".format(
                mode, data_mode, filter_mode))

    def connect(self):
        self.control.handshake()
        self.control_negotiate()
        self.civ.remote_port = self.remote_civ_port
        self.civ.handshake()
        self.open_civ_channel()
        self.set_udig()

    def open_audio(self):
        if not self.udig_verified:
            raise PTTError("U-DIG 未验证，禁止打开发射音频")
        self.audio.remote_port = self.remote_audio_port
        self.audio.handshake()
        if not self.audio.remote_id:
            raise X6200Error("音频握手未提供有效 remote ID")
        self.audio_opened = True
        self.next_audio_ping = self.clock()
        LOG.info("网络音频 AYT/Here/Ready 握手完成")

    def set_ptt(self, transmit, wait_callback=None):
        target = 1 if transmit else 0
        label = "ON" if transmit else "OFF"
        command = build_civ_frame(
            self.radio_civ, b"\x1C\x00" + bytes((target,)))
        self.drain_civ()
        self.last_ptt_state = None
        ack = None
        for attempt in range(2):
            LOG.info("PTT %s 发送：%s", label, command.hex(" "))
            self.send_civ_frame(command)
            try:
                ack = self.wait_ack(wait_callback=wait_callback)
                LOG.info("PTT %s 收到 CI-V 应答：%s", label, ack.hex(" "))
                break
            except ProtocolTimeout as exc:
                if attempt == 1:
                    raise PTTError("PTT {} ACK 超时，电台 TX 状态未知".format(label)) from exc
                LOG.warning("PTT %s ACK 超时，补发一次", label)
            except CIVNack as exc:
                raise PTTError("PTT {} 被电台 FA 拒绝".format(label)) from exc
        if ack is None:
            raise PTTError("PTT {} 未获得 FB".format(label))

        query = build_civ_frame(self.radio_civ, b"\x1C\x00")
        LOG.info("PTT %s 查询电台 TX 状态", label)
        self.send_civ_frame(query)
        next_query_ns = self.clock() + int(CIV_PTT_QUERY_INTERVAL * 1_000_000_000)

        def is_target_ptt_state(frame):
            return (len(frame) >= 7 and frame[2] == CONTROLLER_CIV and
                    frame[3] == self.radio_civ and
                    frame[4:6] == b"\x1C\x00" and frame[6] == target)

        def poll_while_waiting():
            nonlocal next_query_ns
            if wait_callback is not None:
                wait_callback()
            now = self.clock()
            if now >= next_query_ns:
                # 与 wfview 一致持续查询；不能在 ACK 后丢弃已到达的状态帧。
                LOG.info("PTT %s 再次查询电台 TX 状态", label)
                self.send_civ_frame(query)
                next_query_ns = now + int(CIV_PTT_QUERY_INTERVAL * 1_000_000_000)

        try:
            # TX/RX 切换可能先回读旧状态；wfview 实测 TX 回读可滞后约 1.3 秒。
            frame = self._wait_for_civ(
                is_target_ptt_state, CIV_READ_TIMEOUT, poll_while_waiting)
        except ProtocolTimeout as exc:
            if self.last_ptt_state is None:
                detail = "未收到状态回读"
            else:
                detail = "最后回读为 {}".format(
                    "TX" if self.last_ptt_state else "RX")
            raise PTTError(
                "PTT {} 状态查询超时，{}，电台 TX 状态未知".format(
                    label, detail)) from exc
        state = frame[6]
        self.last_ptt_state = state
        if state != target:
            raise PTTError(
                "PTT {} 后电台 TX 状态为 {}，预期 {}".format(label, state, target))
        LOG.info("PTT %s 已获 FB，电台 TX 状态确认：%s", label,
                 "TX" if state else "RX")

    def transmit(self, pcm, max_tx_seconds=30.0, start_ns=None):
        if not self.udig_verified or not self.audio_opened:
            raise PTTError("U-DIG 或音频通道未就绪，禁止 PTT")
        sender = AudioSender(self, pcm)
        sender.validate_duration(max_tx_seconds)
        ptt_attempted = False
        off_confirmed = False
        original_error = None
        try:
            tx_deadline = self.clock() + int(max_tx_seconds * 1_000_000_000)
            sender.next_ns = self.clock() + AUDIO_SEND_AHEAD_NS
            if start_ns is not None:
                # 时隙前持续输出静音，避免电台音频通道因长时间无包而失联；
                # PTT 在时隙前 FT8_PTT_LEAD_NS 秒请求，确认后再按时隙边界发送符号。
                tx_deadline = start_ns + int(max_tx_seconds * 1_000_000_000)
                ptt_at = start_ns - FT8_PTT_LEAD_NS
                while self.clock() < ptt_at:
                    self.service_background(drain_civ=False)
                    sender.pump_silence(drain_civ=False, limit_ns=tx_deadline)
                    self.sleeper(POLL_INTERVAL)
                ptt_attempted = True
                self.set_ptt(True, wait_callback=lambda: sender.pump_silence(
                    drain_civ=False, limit_ns=tx_deadline))
                self.tx_monitor = True
                self.next_tx_query_ns = 0
                lateness_ns = self.clock() - start_ns
                if lateness_ns > PTT_LATE_TOLERANCE_NS:
                    raise ProtocolTimeout(
                        "PTT 确认晚于 FT8 时隙起点 %.3f 秒（容忍 %.1f 秒），中止本次发射" % (
                            lateness_ns / 1_000_000_000,
                            PTT_LATE_TOLERANCE_NS / 1_000_000_000))
                if lateness_ns <= 0:
                    # 提前或准时：泵送静音至时隙边界，符号对齐时隙起点。
                    while self.clock() < start_ns - AUDIO_SEND_AHEAD_NS:
                        self.service_background(drain_civ=False)
                        sender.pump_silence(drain_civ=False, limit_ns=tx_deadline)
                        remaining = start_ns - AUDIO_SEND_AHEAD_NS - self.clock()
                        self.sleeper(min(POLL_INTERVAL, max(0, remaining) / 1_000_000_000))
                    sender.next_ns = start_ns
                    LOG.info("FT8 UTC 时隙开始，发送 79 个符号")
                else:
                    # 迟到但在容忍内：从确认时刻起正常节奏发送，DT 偏移约等于迟到量。
                    sender.next_ns = self.clock() + AUDIO_SEND_AHEAD_NS
                    LOG.warning(
                        "PTT 确认晚于时隙 %.3f 秒（容忍 %.1f 秒），立即开始发射，DT 将偏移约 %.3f 秒",
                        lateness_ns / 1_000_000_000, PTT_LATE_TOLERANCE_NS / 1_000_000_000,
                        lateness_ns / 1_000_000_000)
                    LOG.info("FT8 符号开始（相对时隙 %.3f s），发送 79 个符号",
                             lateness_ns / 1_000_000_000)
            else:
                ptt_attempted = True
                # 先请求 PTT 并确认，确认期间不发送音频，避免音频突发挤占 CI-V ACK。
                self.set_ptt(True)
                self.tx_monitor = True
                self.next_tx_query_ns = 0
                sender.next_ns = self.clock() + AUDIO_SEND_AHEAD_NS
                LOG.info("发送 1 秒静音预热")
                sender.send_pcm(bytes(PREROLL_SAMPLES * 2))
                LOG.info("开始发送 Morse PCM")
            sender.send_pcm(pcm + bytes(TAIL_SAMPLES * 2),
                            limit_ns=tx_deadline)
            self.set_ptt(
                False,
                wait_callback=lambda: sender.pump_silence(drain_civ=False))
            off_confirmed = True
            LOG.info("发射流程结束，电台 TX 状态已确认 RX")
            return sender
        except BaseException as exc:
            original_error = exc
            raise
        finally:
            self.tx_monitor = False
            if ptt_attempted and not off_confirmed:
                cleanup_errors = []
                for attempt in range(3):
                    try:
                        LOG.warning("异常清理：尝试 PTT OFF（%d/3）", attempt + 1)
                        self.set_ptt(False)
                        off_confirmed = True
                        LOG.warning("异常清理：电台 TX 状态已确认 RX")
                        break
                    except BaseException as exc:
                        cleanup_errors.append(exc)
                        LOG.error("异常清理 PTT OFF 失败：%s", exc)
                if not off_confirmed:
                    error = PTTError(
                        "PTT 状态未知：多次 PTT OFF 均未确认；进程将非零退出")
                    if original_error is not None:
                        raise error from original_error
                    if cleanup_errors:
                        raise error from cleanup_errors[-1]
                    raise error

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.civ_opened and self.civ.remote_port:
                try:
                    self.send_civ_open(opened=False)
                except Exception as exc:
                    LOG.debug("CI-V close 发送失败：%s", exc)
            if self.authenticated:
                try:
                    self.send_token(0x01)
                except Exception as exc:
                    LOG.debug("token 移除发送失败：%s", exc)
            links = (self.civ, self.audio, self.control)
            for _ in range(3):
                active = False
                for link in links:
                    if link.remote_id and link.remote_port and not link.remote_closed:
                        active = True
                        try:
                            link.send_control(0x05, 0)
                        except Exception as exc:
                            LOG.debug("%s disconnect 发送失败：%s", link.name, exc)
                if not active:
                    break
                deadline = self.clock() + 250_000_000
                while self.clock() < deadline:
                    for link in links:
                        try:
                            packet = link.recv()
                            if packet is not None:
                                link.housekeeping(packet)
                        except Exception as exc:
                            LOG.debug("%s linger 接收失败：%s", link.name, exc)
                    self.sleeper(0.01)
        finally:
            for link in (self.civ, self.audio, self.control):
                try:
                    link.close()
                except Exception as exc:
                    LOG.debug("%s socket 关闭失败：%s", link.name, exc)


def validate_tx_duration(pcm, max_tx_seconds):
    """在 PTT 前校验 Morse、尾音及最终包对齐后的总时长。"""
    if not math.isfinite(max_tx_seconds) or max_tx_seconds <= 0:
        raise ValueError("max_tx_seconds 必须是有限正数")
    stream_samples = len(pcm) // 2 + TAIL_SAMPLES
    packets = ((stream_samples + AUDIO_SAMPLES_PER_TICK - 1) //
               AUDIO_SAMPLES_PER_TICK)
    required_ns = packets * AUDIO_TICK_PERIOD_NS
    if required_ns > int(max_tx_seconds * 1_000_000_000):
        raise ValueError(
            "音频加 {:.3f} 秒尾音共需 {:.3f} 秒，超过最长发射 {:.3f} 秒；已在 PTT 前拒绝".format(
                TAIL_SAMPLES / SAMPLE_RATE,
                required_ns / 1_000_000_000, max_tx_seconds))
    return required_ns


class AudioSender:
    """48 kHz PCM16LE：每 12.5 ms 调度 600 样本，每个数据报 1200 字节。"""

    def __init__(self, session, pcm, clock=None, sleeper=None):
        if len(pcm) & 1:
            raise ValueError("PCM16LE 字节数必须为偶数")
        self.session = session
        self.pcm = bytes(pcm)
        self.clock = clock or session.clock
        self.sleeper = sleeper or session.sleeper
        self.audio_seq = 0
        # next_ns 是名义时点；实际投递提前 AUDIO_SEND_AHEAD_NS。
        self.next_ns = self.clock() + AUDIO_SEND_AHEAD_NS
        self.packets_sent = 0
        self.late_packets = 0
        self.skipped_slots = 0
        self.errors = 0

    def validate_duration(self, max_tx_seconds):
        return validate_tx_duration(self.pcm, max_tx_seconds)

    def _check_limit(self, limit_ns):
        if limit_ns is not None and self.clock() >= limit_ns:
            raise ProtocolTimeout("达到最长发射时间，立即中止并清理 PTT")

    def _send_due(self, payload, drain_civ, limit_ns):
        now = self.clock()
        self._check_limit(limit_ns)
        deadline = self.next_ns
        lateness = now - (deadline - AUDIO_SEND_AHEAD_NS)
        if lateness < 0:
            return False
        if lateness > MAX_AUDIO_LATENESS_NS:
            raise AudioScheduleError(
                "音频调度严重迟发 {:.1f} ms，禁止突发补发".format(lateness / 1_000_000))
        if lateness >= AUDIO_TICK_PERIOD_NS:
            skipped = lateness // AUDIO_TICK_PERIOD_NS
            self.next_ns += skipped * AUDIO_TICK_PERIOD_NS
            self.skipped_slots += skipped
            self.late_packets += 1
            LOG.warning("音频迟发，跳过 %d 个调度槽而不突发补发", skipped)
            return False
        if lateness:
            self.late_packets += 1
        for offset in range(0, len(payload), AUDIO_MAX_DATAGRAM_PCM):
            part = payload[offset:offset + AUDIO_MAX_DATAGRAM_PCM]
            packet = build_audio_packet(
                self.session.audio.send_seq, self.audio_seq,
                self.session.audio.my_id, self.session.audio.remote_id, part)
            try:
                self.session.audio.send_tracked(packet)
            except BaseException:
                self.errors += 1
                raise
            self.audio_seq = (self.audio_seq + 1) & 0xFFFF
            self.packets_sent += 1
        self.next_ns = deadline + AUDIO_TICK_PERIOD_NS
        return True

    def send_pcm(self, pcm, drain_civ=True, limit_ns=None):
        if len(pcm) & 1:
            raise ValueError("PCM16LE 字节数必须为偶数")
        offset = 0
        while offset < len(pcm):
            block = pcm[offset:offset + AUDIO_PCM_BYTES_PER_TICK]
            if len(block) < AUDIO_PCM_BYTES_PER_TICK:
                block += bytes(AUDIO_PCM_BYTES_PER_TICK - len(block))
            self._check_limit(limit_ns)
            while self.clock() < self.next_ns - AUDIO_SEND_AHEAD_NS:
                if limit_ns is not None and self.next_ns > limit_ns:
                    raise ProtocolTimeout("下一音频包将超过最长发射时间")
                self.session.service_background(drain_civ=drain_civ)
                remaining = self.next_ns - AUDIO_SEND_AHEAD_NS - self.clock()
                self.sleeper(min(POLL_INTERVAL, max(0, remaining) / 1_000_000_000))
            if self._send_due(block, drain_civ, limit_ns):
                offset += len(block)
            else:
                self.session.service_background(drain_civ=drain_civ)

    def pump_silence(self, drain_civ=False, limit_ns=None):
        return self._send_due(bytes(AUDIO_PCM_BYTES_PER_TICK), drain_civ, limit_ns)


def next_ft8_slot(wall_ns, monotonic_ns, lead_ns=FT8_START_LEAD_NS):
    """Return monotonic PCM start and wall time for a slot with enough setup lead."""
    slot_wall_ns = ((wall_ns // FT8_SLOT_PERIOD_NS) + 1) * FT8_SLOT_PERIOD_NS
    start_ns = monotonic_ns + slot_wall_ns - wall_ns
    if start_ns - monotonic_ns < lead_ns:
        slot_wall_ns += FT8_SLOT_PERIOD_NS
        start_ns += FT8_SLOT_PERIOD_NS
    return start_ns, slot_wall_ns


def wait_for_ft8_slot(session):
    start_ns, slot_wall_ns = next_ft8_slot(time.time_ns(), session.clock())
    LOG.info("FT8 目标时隙：%s", time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(slot_wall_ns / 1_000_000_000)))
    # 不在等待期间空转：立即返回，让 transmit() 从当前时刻持续填充静音，
    # 避免电台音频通道因长时间没有数据而进入异常状态。
    return start_ns


def _positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须是有限正数")
    return number


def build_argument_parser():
    parser = argparse.ArgumentParser(
        description="本机生成 CW/FT8 PCM，经 X6200 U-DIG 网络音频发射；默认不联网")
    parser.add_argument("text", nargs="?", help="要生成或发送的 CW/FT8 文本")
    parser.add_argument("--host", default="192.168.31.99")
    parser.add_argument("--user", default="user")
    parser.add_argument("--password", default="123")
    parser.add_argument("--wpm", type=_positive, default=12.0)
    parser.add_argument("--tone", type=_positive, default=600.0)
    parser.add_argument("--max-tx-seconds", type=_positive, default=30.0)
    parser.add_argument("--wav", help="额外写入 48 kHz/mono/PCM16LE WAV")
    parser.add_argument("--ft8", action="store_true",
                        help="发送已验证的 CQ BH1WQR OM89，并对齐 15 秒 UTC 时隙")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mode-only", action="store_true",
                      help="只登录、设置并回读 U-DIG，不生成音频或 PTT")
    mode.add_argument("--tx", action="store_true",
                      help="实际请求 PTT 并发送网络音频（默认仅 dry-run）")
    return parser


def _load_morse():
    from morse import generate_pcm, write_wav
    return generate_pcm, write_wav


def _load_ft8():
    from ft8 import generate_pcm
    from morse import write_wav
    return generate_pcm, write_wav


def main(argv=None):
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.mode_only and (args.text is not None or args.wav or args.ft8):
        parser.error("--mode-only 不接受文本、--wav 或 --ft8")
    if not args.mode_only and args.text is None:
        parser.error("请提供 CW/FT8 文本，或使用 --mode-only")
    if not args.mode_only and not args.text.strip():
        parser.error("CW 文本不能只包含空白字符")
    if args.tone >= SAMPLE_RATE / 2:
        parser.error("--tone 必须低于 24000 Hz")

    pcm = None
    sender = None
    session = None
    try:
        if not args.mode_only:
            generate_pcm, write_wav = _load_ft8() if args.ft8 else _load_morse()
            if args.ft8:
                pcm = generate_pcm(args.text, sample_rate=SAMPLE_RATE, amplitude=0.5)
            else:
                pcm = generate_pcm(
                    args.text, wpm=args.wpm, tone_hz=args.tone,
                    sample_rate=SAMPLE_RATE, amplitude=0.5)
            if len(pcm) & 1:
                raise ValueError("音频生成器返回了非 PCM16LE 字节流")
            if args.wav:
                write_wav(args.wav, pcm, sample_rate=SAMPLE_RATE)
                LOG.info("WAV 已写入 %s", args.wav)
            if not args.tx:
                print("{} dry-run：未联网、未 PTT；PCM {} 字节，时长 {:.3f} 秒".format(
                    "FT8" if args.ft8 else "CW",
                    len(pcm), len(pcm) / 2 / SAMPLE_RATE))
                return 0
            validate_tx_duration(pcm, args.max_tx_seconds)

        ensure_no_wfview()
        session = X6200Session.create(
            args.host, args.user, args.password)
        session.connect()
        if args.mode_only:
            print("U-DIG 设置及 01/01/01 回读完成；未发送音频，未请求 PTT")
            return 0
        session.open_audio()
        if args.ft8:
            start_ns = wait_for_ft8_slot(session)
            sender = session.transmit(
                pcm, args.max_tx_seconds, start_ns=start_ns)
        else:
            sender = session.transmit(pcm, args.max_tx_seconds)
        print("网络音频发送完成：packets={} late={} skipped={} errors={} rx_audio={}".format(
            sender.packets_sent, sender.late_packets, sender.skipped_slots,
            sender.errors, session.rx_audio_packets))
        return 0
    except KeyboardInterrupt:
        LOG.error("收到 Ctrl-C，已执行可用的 PTT 清理")
        return 130
    except (X6200Error, OSError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    sys.exit(main())
