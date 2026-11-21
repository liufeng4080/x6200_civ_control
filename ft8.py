"""Generate the verified FT8 test message as mono PCM16LE."""

import array
import math
import sys


MESSAGE = "CQ BH1WQR OM89"
# WSJT-X ft8code reference output for MESSAGE.
TONES = tuple(int(value) for value in (
    "314065200000000106611113061572432142"
    "3140652701375147255302160170416633323140652"
))
SYMBOL_SECONDS = 0.160
TONE_STEP_HZ = 6.25


def generate_pcm(text=MESSAGE, sample_rate=48000, base_hz=1500.0, amplitude=0.5):
    """Return the single validated FT8 test message as 79 CPFSK symbols."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if " ".join(text.upper().split()) != MESSAGE:
        raise ValueError("当前 FT8 测试仅支持：{}".format(MESSAGE))
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("sample_rate 必须是正整数")
    if not math.isfinite(base_hz) or base_hz <= 0:
        raise ValueError("base_hz 必须是有限正数")
    if base_hz + max(TONES) * TONE_STEP_HZ >= sample_rate / 2:
        raise ValueError("FT8 最高音调必须低于 Nyquist 频率")
    if not math.isfinite(amplitude) or not 0 <= amplitude <= 1:
        raise ValueError("amplitude 必须在 0 到 1 之间")

    symbol_samples = round(SYMBOL_SECONDS * sample_rate)
    scale = round(32767 * amplitude)
    two_pi = 2 * math.pi
    phase = 0.0
    pcm = array.array("h")
    if pcm.itemsize != 2:
        raise RuntimeError("array('h') is not 16-bit on this platform")
    for tone in TONES:
        phase_step = two_pi * (base_hz + tone * TONE_STEP_HZ) / sample_rate
        for _ in range(symbol_samples):
            phase = (phase + phase_step) % two_pi
            pcm.append(round(scale * math.sin(phase)))
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()
