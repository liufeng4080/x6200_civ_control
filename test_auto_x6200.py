import array
import hashlib
import math
import os
import struct
import tempfile
import unittest
from unittest import mock
import wave

from morse import generate_pcm, write_wav
import ft8
import x6200


SAMPLE_RATE = 48000


def pcm_samples(pcm):
    samples = array.array("h")
    samples.frombytes(pcm)
    if os.sys.byteorder != "little":
        samples.byteswap()
    return samples


class MorseTests(unittest.TestCase):
    def assert_duration(self, text, dots):
        expected = round(dots * 1.2 / 12 * SAMPLE_RATE)
        pcm = generate_pcm(text, wpm=12, sample_rate=SAMPLE_RATE)
        self.assertEqual(len(pcm), expected * 2)

    def test_single_dot_and_dash(self):
        self.assert_duration("E", 1)
        self.assert_duration("T", 3)

    def test_internal_character_and_word_gaps(self):
        self.assert_duration("S", 5)
        self.assert_duration("EE", 5)
        self.assert_duration("E E", 9)

    def test_test_message_duration(self):
        self.assert_duration("TEST", 21)

    def test_unknown_character_is_rejected(self):
        with self.assertRaises(ValueError):
            generate_pcm("TEST$", wpm=12)
        with self.assertRaises(ValueError):
            generate_pcm("你", wpm=12)

    def test_pcm_is_bounded_and_has_tone(self):
        samples = pcm_samples(generate_pcm("E", amplitude=0.5))
        peak = max(abs(value) for value in samples)
        self.assertGreater(peak, 15000)
        self.assertLessEqual(peak, round(0.5 * 32767) + 1)
        self.assertLess(abs(samples[0]), 100)
        self.assertLess(abs(samples[-1]), 100)

    def test_phase_is_continuous_across_character_gap(self):
        wpm = 12
        tone_hz = 601
        pcm = generate_pcm("EE", wpm=wpm, tone_hz=tone_hz)
        samples = pcm_samples(pcm)
        dot_samples = round(1.2 / wpm * SAMPLE_RATE)
        gap_samples = round(3 * 1.2 / wpm * SAMPLE_RATE)
        second_start = dot_samples + gap_samples
        ramp_samples = round(0.005 * SAMPLE_RATE)
        index = second_start + ramp_samples + 10
        envelope = 1.0
        expected = round(0.5 * 32767 * envelope * math.sin(
            2 * math.pi * tone_hz * index / SAMPLE_RATE
        ))
        self.assertLess(abs(samples[index] - expected), 2)

    def test_wav_format(self):
        pcm = generate_pcm("E")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cw.wav")
            write_wav(path, pcm)
            with wave.open(path, "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getframerate(), SAMPLE_RATE)
                self.assertEqual(wav.getnframes(), len(pcm) // 2)


class FT8Tests(unittest.TestCase):
    def test_verified_message_matches_independently_decoded_pcm(self):
        pcm = ft8.generate_pcm()
        self.assertEqual(len(ft8.TONES), 79)
        self.assertEqual(ft8.TONES[:7], (3, 1, 4, 0, 6, 5, 2))
        self.assertEqual(ft8.TONES[36:43], (3, 1, 4, 0, 6, 5, 2))
        self.assertEqual(ft8.TONES[72:], (3, 1, 4, 0, 6, 5, 2))
        self.assertEqual(len(pcm), 79 * 7680 * 2)
        self.assertEqual(
            hashlib.sha256(pcm).hexdigest(),
            "d6da8e494752de9b7733c158574dd4910471852fe378cd5cc04c188501aec9a1")

    def test_unverified_ft8_message_is_rejected(self):
        with self.assertRaises(ValueError):
            ft8.generate_pcm("CQ TEST OM89")


class FakeClock:
    def __init__(self, now_ns=0):
        self.now_ns = now_ns

    def __call__(self):
        return self.now_ns

    def sleep(self, seconds):
        self.now_ns += max(0, round(seconds * 1_000_000_000))


class RecordingLink:
    def __init__(self, my_id=0x11223344, remote_id=0x55667788,
                 local_port=50002, clock=None):
        self.my_id = my_id
        self.remote_id = remote_id
        self.local_port = local_port
        self.clock = clock
        self.send_seq = 0
        self.sent = []
        self.sent_times = []
        self.tracked = []

    def send_raw(self, packet, remote_port=None):
        self.sent.append(bytes(packet))
        self.sent_times.append(self.clock() if self.clock else None)

    def send_tracked(self, packet):
        packet = bytearray(packet)
        struct.pack_into("<H", packet, 6, self.send_seq)
        cached = bytes(packet)
        self.tracked.append(cached)
        self.send_seq = (self.send_seq + 1) & 0xFFFF
        self.send_raw(cached)


class FakeSocket:
    def __init__(self, incoming=(), local_port=50001):
        self.incoming = list(incoming)
        self.local_port = local_port
        self.sent = []
        self.closed = False

    def getsockname(self):
        return ("192.168.31.20", self.local_port)

    def sendto(self, packet, destination):
        self.sent.append((bytes(packet), destination))
        return len(packet)

    def recvfrom(self, size):
        if not self.incoming:
            raise BlockingIOError()
        packet, source = self.incoming.pop(0)
        return packet, source

    def close(self):
        self.closed = True


class PacketTests(unittest.TestCase):
    def test_audio_packet_golden_bytes(self):
        packet = x6200.build_audio_packet(
            0x1234, 0xABCD, 0x11223344, 0x55667788,
            b"\x01\x02\x03\x04")
        expected_header = bytes.fromhex(
            "1c000000"  # LE32 total length: 28
            "0000"      # type
            "3412"      # LE16 outer sequence
            "44332211"  # LE32 local ID
            "88776655"  # LE32 remote ID
            "8000"      # LE16 0x0080
            "abcd"      # BE16 audio sequence
            "0000"      # reserved
            "0004"      # BE16 PCM byte count
        )
        self.assertEqual(packet, expected_header + b"\x01\x02\x03\x04")
        self.assertEqual(len(packet), 28)
        with self.assertRaises(ValueError):
            x6200.build_audio_packet(0, 0, 1, 2, b"\x00")

    def test_audio_packet_is_cached_and_retransmitted(self):
        clock = FakeClock()
        sock = FakeSocket(local_port=50003)
        link = x6200.UDPLink(
            "audio", "192.168.31.99", 50003, sock, "192.168.31.20",
            clock=clock, sleeper=clock.sleep)
        packet = x6200.build_audio_packet(
            0, 0, link.my_id, 0x55667788, b"\x01\x02")
        link.send_tracked(packet)

        self.assertEqual(link.tx_cache.maxlen, 500)
        self.assertTrue(link.retransmit(0))
        self.assertEqual(len(sock.sent), 2)
        self.assertEqual(sock.sent[0][0], sock.sent[1][0])
        self.assertEqual(struct.unpack_from("<H", sock.sent[0][0], 6)[0], 0)

    def test_civ_wrappers_and_sequence_wrap(self):
        opened = x6200.build_civ_open_packet(1, 2, 0x10000)
        self.assertEqual(len(opened), 0x16)
        self.assertEqual(struct.unpack_from("<H", opened, 0x10)[0], 0x01C0)
        self.assertEqual(struct.unpack_from(">H", opened, 0x13)[0], 0)
        self.assertEqual(opened[0x15], 4)

        frame = x6200.build_civ_frame(0xA4, b"\x1C\x00\x01")
        self.assertEqual(frame, bytes.fromhex("fefea4e11c0001fd"))
        wrapped = x6200.build_civ_data_packet(frame, 1, 2, 0x10001)
        self.assertEqual(struct.unpack_from(">H", wrapped, 0x13)[0], 1)
        self.assertEqual(x6200.parse_civ_datagram(wrapped), [frame])

    def test_civ_parser_handles_noise_and_multiple_frames(self):
        first = x6200.build_civ_frame(0xA4, b"\x26\x00")
        second = b"\xFE\xFE\xE1\xA4\xFB\xFD"
        payload = b"noise" + first + second + b"tail"
        self.assertEqual(list(x6200.iter_civ_frames(payload)), [first, second])
        datagram = x6200.build_civ_data_packet(first + second, 1, 2, 3)
        self.assertEqual(x6200.parse_civ_datagram(datagram), [first, second])
        with self.assertRaises(ValueError):
            x6200.parse_civ_datagram(datagram[:-1])

    def test_stream_request_fields(self):
        control = RecordingLink(local_port=50001)
        civ = RecordingLink(local_port=51002)
        audio = RecordingLink(local_port=51003)
        session = x6200.X6200Session(control, civ, audio)
        session.radio_identity = bytes(range(16))
        session.radio_name = "X6100"
        session.tok_request = 0x1234
        session.token = 0x89ABCDEF
        session.send_stream_request()

        self.assertEqual(len(control.tracked), 1)
        packet = control.tracked[0]
        self.assertEqual(len(packet), 0x90)
        self.assertEqual(struct.unpack_from("<I", packet, 0)[0], 0x90)
        self.assertEqual(struct.unpack_from(">I", packet, 0x10)[0], 0x80)
        self.assertEqual(packet[0x15], 3)
        self.assertEqual(struct.unpack_from("<H", packet, 0x1A)[0], 0x1234)
        self.assertEqual(struct.unpack_from("<I", packet, 0x1C)[0], 0x89ABCDEF)
        self.assertEqual(packet[0x20:0x30], bytes(range(16)))
        self.assertEqual(packet[0x40:0x45], b"X6100")
        self.assertEqual(packet[0x60:0x70], x6200.encode_passcode("user"))
        self.assertEqual(packet[0x70:0x74], b"\x01\x01\x04\x04")
        self.assertEqual(struct.unpack_from(">I", packet, 0x74)[0], 8000)
        self.assertEqual(struct.unpack_from(">I", packet, 0x78)[0], 48000)
        self.assertEqual(struct.unpack_from(">I", packet, 0x7C)[0], 51002)
        self.assertEqual(struct.unpack_from(">I", packet, 0x80)[0], 51003)
        self.assertEqual(struct.unpack_from(">I", packet, 0x84)[0],
                         x6200.TX_LATENCY_MS)
        self.assertEqual(packet[0x88], 1)

    def test_udp_handshake_and_tracked_sequence_wrap(self):
        host = "192.168.31.99"
        here = x6200.build_control_packet(0xAABBCCDD, 0, 0x04)
        ready = x6200.build_control_packet(0xAABBCCDD, 0, 0x06)
        sock = FakeSocket(((here, (host, 50001)), (ready, (host, 50001))))
        clock = FakeClock()
        link = x6200.UDPLink("control", host, 50001, sock,
                             "192.168.31.20", clock=clock, sleeper=clock.sleep)
        link.send_seq = 0xFFFF
        link.handshake(timeout=0.01)
        self.assertEqual(link.remote_id, 0xAABBCCDD)
        sent_types = [struct.unpack_from("<H", packet, 4)[0]
                      for packet, destination in sock.sent]
        self.assertEqual(sent_types, [0x03, 0x06])

        link.send_tracked(x6200.build_control_packet(1, 2, 0))
        self.assertEqual(link.send_seq, 0)
        self.assertEqual(struct.unpack_from("<H", sock.sent[-1][0], 6)[0], 0xFFFF)


class AudioSenderTests(unittest.TestCase):
    def make_sender(self, pcm=b"", now_ns=1_000_000_000):
        clock = FakeClock(now_ns)
        audio = RecordingLink(clock=clock)
        session = mock.Mock()
        session.clock = clock
        session.sleeper = clock.sleep
        session.audio = audio
        session.service_background = mock.Mock()
        sender = x6200.AudioSender(session, pcm)
        return sender, audio, clock, session

    def test_schedule_packet_sizes_padding_and_sequence_wrap(self):
        pcm = bytes(x6200.AUDIO_PCM_BYTES_PER_TICK * 2 + 600)
        sender, audio, clock, session = self.make_sender(pcm)
        audio.send_seq = 0xFFFF
        sender.audio_seq = 0xFFFF
        sender.send_pcm(pcm)

        self.assertEqual(x6200.AUDIO_TICK_PERIOD_NS, 12_500_000)
        self.assertEqual(len(audio.sent), 3)
        self.assertEqual(audio.sent_times, [
            1_000_000_000,
            1_012_500_000,
            1_025_000_000,
        ])
        self.assertTrue(all(len(packet) == 1224 for packet in audio.sent))
        self.assertEqual([struct.unpack_from("<H", packet, 6)[0]
                          for packet in audio.sent], [0xFFFF, 0, 1])
        self.assertEqual([struct.unpack_from(">H", packet, 0x12)[0]
                          for packet in audio.sent], [0xFFFF, 0, 1])
        self.assertEqual(struct.unpack_from(">H", audio.sent[-1], 0x16)[0], 1200)
        self.assertEqual(audio.sent[-1][24 + 600:], bytes(600))
        self.assertEqual(sender.packets_sent, 3)
        self.assertEqual(sender.errors, 0)
        self.assertEqual(audio.tracked, audio.sent)
        self.assertEqual(audio.send_seq, 2)

    def test_lateness_skips_slots_without_burst_and_severe_lateness_aborts(self):
        sender, audio, clock, session = self.make_sender()
        # 实际投递点是 next_ns - AUDIO_SEND_AHEAD_NS，迟发按该点计算。
        due = sender.next_ns - x6200.AUDIO_SEND_AHEAD_NS
        clock.now_ns = due + 2 * x6200.AUDIO_TICK_PERIOD_NS
        self.assertFalse(sender.pump_silence())
        self.assertEqual(sender.skipped_slots, 2)
        self.assertEqual(len(audio.sent), 0)
        self.assertTrue(sender.pump_silence())
        self.assertEqual(len(audio.sent), 1)

        late_sender, late_audio, late_clock, unused = self.make_sender()
        late_due = late_sender.next_ns - x6200.AUDIO_SEND_AHEAD_NS
        late_clock.now_ns = late_due + x6200.MAX_AUDIO_LATENESS_NS + 1
        with self.assertRaises(x6200.AudioScheduleError):
            late_sender.pump_silence()
        self.assertEqual(len(late_audio.sent), 0)

    def test_socket_error_is_counted_and_propagated(self):
        sender, audio, clock, session = self.make_sender()
        clock.now_ns = sender.next_ns - x6200.AUDIO_SEND_AHEAD_NS
        audio.send_raw = mock.Mock(side_effect=OSError("send failed"))
        with self.assertRaises(OSError):
            sender.pump_silence()
        self.assertEqual(sender.errors, 1)
        self.assertEqual(sender.packets_sent, 0)

    def test_duration_validation_includes_tail_and_packet_alignment(self):
        pcm = bytes(9600)  # 0.1 seconds at 48 kHz PCM16LE.
        required = x6200.validate_tx_duration(pcm, 1.1)
        self.assertEqual(required, 1_100_000_000)
        with self.assertRaises(ValueError):
            x6200.validate_tx_duration(pcm, 1.099)


class CIVSafetyTests(unittest.TestCase):
    def make_session(self):
        session = object.__new__(x6200.X6200Session)
        session.radio_civ = 0xA4
        session.last_ptt_state = None
        session.udig_verified = False
        session.clock = FakeClock()
        session.sleeper = session.clock.sleep
        session.drain_civ = mock.Mock()
        session.send_civ_frame = mock.Mock()
        return session

    def test_udig_readback_success_and_nack_still_requires_readback(self):
        response = bytes.fromhex("fefee1a42600010101fd")
        for ack in (bytes.fromhex("fefee1a4fbfd"), x6200.CIVNack("NACK")):
            with self.subTest(ack=ack):
                session = self.make_session()
                if isinstance(ack, Exception):
                    session.wait_ack = mock.Mock(side_effect=ack)
                else:
                    session.wait_ack = mock.Mock(return_value=ack)
                session._wait_for_civ = mock.Mock(return_value=response)
                session.set_udig()
                self.assertTrue(session.udig_verified)
                self.assertEqual(session.send_civ_frame.call_count, 2)

    def test_udig_wrong_readback_or_timeout_forbids_transmit(self):
        for response in (bytes.fromhex("fefee1a42600010001fd"),
                         x6200.ProtocolTimeout("timeout")):
            with self.subTest(response=response):
                session = self.make_session()
                session.wait_ack = mock.Mock(
                    return_value=bytes.fromhex("fefee1a4fbfd"))
                if isinstance(response, Exception):
                    session._wait_for_civ = mock.Mock(side_effect=response)
                else:
                    session._wait_for_civ = mock.Mock(return_value=response)
                with self.assertRaises(x6200.PTTError):
                    session.set_udig()
                self.assertFalse(session.udig_verified)

        session.audio_opened = True
        session.set_ptt = mock.Mock()
        with self.assertRaises(x6200.PTTError):
            session.transmit(b"\x00\x00")
        session.set_ptt.assert_not_called()

    def test_ptt_success_requires_ack_and_matching_state(self):
        session = self.make_session()
        session.wait_ack = mock.Mock(
            return_value=bytes.fromhex("fefee1a4fbfd"))
        session._wait_for_civ = mock.Mock(
            return_value=bytes.fromhex("fefee1a41c0001fd"))
        session.set_ptt(True)
        self.assertEqual(session.last_ptt_state, 1)
        self.assertEqual(session.send_civ_frame.call_args_list, [
            mock.call(x6200.build_civ_frame(0xA4, b"\x1C\x00\x01")),
            mock.call(x6200.build_civ_frame(0xA4, b"\x1C\x00")),
        ])

    def test_ptt_waits_for_target_after_transitional_state(self):
        session = self.make_session()
        session.wait_ack = mock.Mock(
            return_value=bytes.fromhex("fefee1a4fbfd"))
        transitional = bytes.fromhex("fefee1a41c0000fd")
        target = bytes.fromhex("fefee1a41c0001fd")

        def wait_for_target(predicate, timeout, wait_callback):
            self.assertFalse(predicate(transitional))
            self.assertTrue(predicate(target))
            return target

        session._wait_for_civ = wait_for_target
        session.set_ptt(True)
        self.assertEqual(session.last_ptt_state, 1)

    def test_ptt_requeries_each_second_without_draining_state_frames(self):
        session = self.make_session()
        session.wait_ack = mock.Mock(
            return_value=bytes.fromhex("fefee1a4fbfd"))
        keep_silence = mock.Mock()
        target = bytes.fromhex("fefee1a41c0001fd")

        def wait_for_target(predicate, timeout, wait_callback):
            for _ in range(3):
                wait_callback()
                session.clock.sleep(1)
            return target

        session._wait_for_civ = wait_for_target
        session.set_ptt(True, wait_callback=keep_silence)
        query = x6200.build_civ_frame(0xA4, b"\x1C\x00")
        self.assertEqual(session.send_civ_frame.call_args_list, [
            mock.call(x6200.build_civ_frame(0xA4, b"\x1C\x00\x01")),
            mock.call(query), mock.call(query), mock.call(query),
        ])
        self.assertEqual(keep_silence.call_count, 3)
        session.drain_civ.assert_called_once_with()

    def test_ptt_nack_timeout_and_state_mismatch_are_failures(self):
        session = self.make_session()
        session.wait_ack = mock.Mock(side_effect=x6200.CIVNack("NACK"))
        session._wait_for_civ = mock.Mock()
        with self.assertRaises(x6200.PTTError):
            session.set_ptt(True)
        session._wait_for_civ.assert_not_called()

        session = self.make_session()
        session.wait_ack = mock.Mock(
            side_effect=x6200.ProtocolTimeout("timeout"))
        with self.assertRaises(x6200.PTTError):
            session.set_ptt(True)
        self.assertEqual(session.wait_ack.call_count, 2)
        self.assertEqual(session.send_civ_frame.call_count, 2)

        session = self.make_session()
        session.wait_ack = mock.Mock(
            return_value=bytes.fromhex("fefee1a4fbfd"))
        session._wait_for_civ = mock.Mock(
            return_value=bytes.fromhex("fefee1a41c0000fd"))
        with self.assertRaises(x6200.PTTError):
            session.set_ptt(True)

    def make_watchdog_session(self):
        session = self.make_session()
        session.keepalive = mock.Mock()
        session.drain_control = mock.Mock()
        session.drain_audio = mock.Mock()
        session.tx_monitor = False
        session.ptt_ack_seen = False
        session.last_ptt_state = None
        session.ptt_watchdog_ns = None
        session.ptt_retry_ns = None
        session.next_tx_query_ns = 0
        return session

    def test_ptt_on_fire_sends_on_and_arms_watchdog(self):
        session = self.make_session()
        session.ptt_ack_seen = True
        session.last_ptt_state = 1
        session.ptt_watchdog_ns = None
        session.tx_monitor = True
        session.ptt_on_fire()
        session.send_civ_frame.assert_called_once_with(
            x6200.build_civ_frame(0xA4, b"\x1C\x00\x01"))
        session.drain_civ.assert_called_once_with()
        self.assertFalse(session.ptt_ack_seen)
        self.assertIsNone(session.last_ptt_state)
        self.assertEqual(session.ptt_watchdog_ns,
                         session.clock() + x6200.PTT_ACK_WATCHDOG_NS)
        self.assertEqual(session.ptt_retry_ns,
                         session.clock() + int(x6200.CIV_ACK_TIMEOUT * 1_000_000_000))
        self.assertFalse(session.tx_monitor)

    def test_ptt_watchdog_disarms_on_ack_or_tx_readback(self):
        for confirm in ("ack", "tx"):
            with self.subTest(confirm=confirm):
                session = self.make_watchdog_session()
                session.ptt_watchdog_ns = (
                    session.clock() + x6200.PTT_ACK_WATCHDOG_NS)
                if confirm == "ack":
                    session.ptt_ack_seen = True
                else:
                    session.last_ptt_state = 1
                session.service_background()
                self.assertIsNone(session.ptt_watchdog_ns)
                self.assertIsNone(session.ptt_retry_ns)
                self.assertTrue(session.tx_monitor)

    def test_ptt_watchdog_pending_before_timeout(self):
        session = self.make_watchdog_session()
        session.ptt_watchdog_ns = session.clock() + x6200.PTT_ACK_WATCHDOG_NS
        session.service_background()
        self.assertIsNotNone(session.ptt_watchdog_ns)

    def test_ptt_watchdog_retries_without_tx_query_or_blocking(self):
        session = self.make_watchdog_session()
        session.tx_monitor = True
        session.ptt_watchdog_ns = session.clock() + x6200.PTT_ACK_WATCHDOG_NS
        session.ptt_retry_ns = session.clock() + int(
            x6200.CIV_ACK_TIMEOUT * 1_000_000_000)
        session.service_background()
        session.send_civ_frame.assert_not_called()
        session.clock.sleep(2)
        session.service_background()
        session.send_civ_frame.assert_called_once_with(
            x6200.build_civ_frame(0xA4, b"\x1C\x00\x01"))
        self.assertIsNone(session.ptt_retry_ns)
        self.assertIsNotNone(session.ptt_watchdog_ns)

    def test_ptt_watchdog_timeout_raises(self):
        session = self.make_watchdog_session()
        session.ptt_watchdog_ns = session.clock() + x6200.PTT_ACK_WATCHDOG_NS
        session.clock.sleep(5)
        with self.assertRaisesRegex(x6200.PTTError, "4 秒"):
            session.service_background()
        self.assertIsNone(session.ptt_watchdog_ns)
        self.assertIsNone(session.ptt_retry_ns)


class FT8TimingTests(unittest.TestCase):
    def test_slot_skips_when_setup_lead_is_insufficient(self):
        start, wall = x6200.next_ft8_slot(
            11_000_000_000, 100_000_000_000, lead_ns=4_000_000_000)
        self.assertEqual(wall, 15_000_000_000)
        self.assertEqual(start, 104_000_000_000)

        start, wall = x6200.next_ft8_slot(
            11_000_000_001, 100_000_000_000, lead_ns=4_000_000_000)
        self.assertEqual(wall, 30_000_000_000)
        self.assertEqual(start, 118_999_999_999)

    def make_session(self, events, clock):
        session = object.__new__(x6200.X6200Session)
        session.udig_verified = True
        session.audio_opened = True
        session.clock = clock
        session.sleeper = clock.sleep
        session.service_background = mock.Mock()
        # 新逻辑：时隙起点 ptt_on_fire 立即 PTT（不等 ACK），set_ptt 仅用于 PTT OFF。
        session.ptt_on_fire = lambda: events.append("ptt_on")

        def set_ptt(transmit, wait_callback=None):
            events.append("ptt_on" if transmit else "ptt_off")

        session.set_ptt = set_ptt
        return session

    def make_sender(self, events, clock):
        sender = mock.Mock()
        sender.next_ns = clock()

        def send_pcm(pcm, drain_civ=True, limit_ns=None):
            events.append(("ft8", clock(), sender.next_ns))

        sender.send_pcm.side_effect = send_pcm
        return sender

    def test_ft8_pcm_starts_on_slot_boundary(self):
        events = []
        clock = FakeClock()
        session = self.make_session(events, clock)
        sender = self.make_sender(events, clock)
        with mock.patch.object(x6200, "AudioSender", return_value=sender):
            session.transmit(b"\x01\x02", start_ns=4_000_000_000)
        # 时隙起点立即 PTT（不等 ACK），符号名义时点对齐时隙边界。
        self.assertEqual(events, [
            "ptt_on",
            ("ft8", 4_000_000_000 - x6200.AUDIO_SEND_AHEAD_NS, 4_000_000_000),
            "ptt_off",
        ])


class TransmitCleanupTests(unittest.TestCase):
    def make_session(self, events, ptt_side_effect=None):
        session = object.__new__(x6200.X6200Session)
        session.udig_verified = True
        session.audio_opened = True
        session.clock = lambda: 0

        def set_ptt(transmit, wait_callback=None):
            events.append("ptt_on" if transmit else "ptt_off")
            if ptt_side_effect is not None:
                ptt_side_effect(transmit)

        session.set_ptt = set_ptt
        return session

    def run_transmit(self, session, events, failure=None):
        class StubSender:
            def __init__(self, unused_session, pcm):
                self.pcm = pcm
                self.packets_sent = 0
                self.late_packets = 0
                self.skipped_slots = 0
                self.errors = 0

            def validate_duration(self, maximum):
                return 0

            def send_pcm(self, pcm, drain_civ=True, limit_ns=None):
                if len(pcm) == x6200.PREROLL_SAMPLES * 2:
                    events.append("preroll")
                    return
                events.append("morse")
                if failure is not None:
                    raise failure

            def pump_silence(self, drain_civ=False, limit_ns=None):
                return False

        with mock.patch.object(x6200, "AudioSender", StubSender):
            return session.transmit(b"\x01\x02")

    def test_normal_order_and_exception_always_turn_ptt_off(self):
        for failure in (None, OSError("audio failed"), KeyboardInterrupt()):
            with self.subTest(failure=failure):
                events = []
                session = self.make_session(events)
                if failure is None:
                    self.run_transmit(session, events)
                else:
                    with self.assertRaises(type(failure)):
                        self.run_transmit(session, events, failure=failure)
                self.assertEqual(events, ["ptt_on", "preroll", "morse", "ptt_off"])

    def test_ptt_on_failure_still_attempts_off(self):
        events = []

        def side_effect(transmit):
            if transmit:
                raise x6200.PTTError("ON failed")

        session = self.make_session(events, side_effect)
        with self.assertRaisesRegex(x6200.PTTError, "ON failed"):
            self.run_transmit(session, events)
        self.assertEqual(events, ["ptt_on", "ptt_off"])

    def test_three_failed_off_attempts_report_unknown_state(self):
        events = []

        def side_effect(transmit):
            if not transmit:
                raise x6200.PTTError("OFF failed")

        session = self.make_session(events, side_effect)
        with self.assertRaisesRegex(x6200.PTTError, "PTT 状态未知"):
            self.run_transmit(session, events, failure=OSError("audio failed"))
        self.assertEqual(events.count("ptt_off"), 3)


class CLITests(unittest.TestCase):
    def test_dry_run_never_connects(self):
        with mock.patch.object(x6200.X6200Session, "create") as create, \
                mock.patch.object(x6200, "ensure_no_wfview") as check:
            self.assertEqual(x6200.main(["E"]), 0)
        create.assert_not_called()
        check.assert_not_called()

    def test_blank_text_is_rejected_before_network(self):
        for text in ("", "   "):
            with self.subTest(text=text), \
                    mock.patch.object(x6200.X6200Session, "create") as create:
                with self.assertRaises(SystemExit) as raised:
                    x6200.main([text, "--tx"])
                self.assertEqual(raised.exception.code, 2)
                create.assert_not_called()

    def test_ft8_dry_run_never_connects(self):
        with mock.patch.object(x6200.X6200Session, "create") as create, \
                mock.patch.object(x6200, "ensure_no_wfview") as check:
            self.assertEqual(x6200.main(["CQ BH1WQR OM89", "--ft8"]), 0)
        create.assert_not_called()
        check.assert_not_called()

    def test_ft8_tx_uses_slot_scheduler(self):
        session = mock.Mock()
        generate = mock.Mock(return_value=b"\x00\x00")
        write_wav = mock.Mock()
        with mock.patch.object(x6200, "_load_ft8", return_value=(generate, write_wav)), \
                mock.patch.object(x6200.X6200Session, "create", return_value=session), \
                mock.patch.object(x6200, "ensure_no_wfview"), \
                mock.patch.object(x6200, "wait_for_ft8_slot", return_value=123) as wait:
            self.assertEqual(x6200.main(["CQ BH1WQR OM89", "--ft8", "--tx"]), 0)
        wait.assert_called_once_with(session)
        session.transmit.assert_called_once_with(b"\x00\x00", 30.0, start_ns=123)

    def test_mode_only_never_opens_audio_or_transmits(self):
        session = mock.Mock()
        with mock.patch.object(x6200.X6200Session, "create", return_value=session), \
                mock.patch.object(x6200, "ensure_no_wfview"):
            self.assertEqual(x6200.main(["--mode-only"]), 0)
        session.connect.assert_called_once_with()
        session.open_audio.assert_not_called()
        session.transmit.assert_not_called()
        session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
