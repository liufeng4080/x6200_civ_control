"""Generate keyed Morse audio as mono 16-bit little-endian PCM."""

import array
import math
import numbers
import os
import sys
import wave

__all__ = ["generate_pcm", "write_wav"]

_MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".",
    "F": "..-.", "G": "--.", "H": "....", "I": "..", "J": ".---",
    "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---",
    "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-",
    "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--",
    "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--",
    "4": "....-", "5": ".....", "6": "-....", "7": "--...",
    "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "'": ".----.",
    "!": "-.-.--", "/": "-..-.", "(": "-.--.", ")": "-.--.-",
    "&": ".-...", ":": "---...", ";": "-.-.-.", "=": "-...-",
    "+": ".-.-.", "-": "-....-", "_": "..--.-", '"': ".-..-.",
    "@": ".--.-.",
}


def _real_parameter(name, value):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"{name} must be a real number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _validate_sample_rate(sample_rate):
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
        raise TypeError("sample_rate must be an integer")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return sample_rate


def _segment_samples(dot_seconds, multiplier, sample_rate):
    return max(1, int(round(dot_seconds * multiplier * sample_rate)))


def generate_pcm(text, wpm=12, tone_hz=600, sample_rate=48000, amplitude=0.5):
    """Return Morse code for *text* as mono PCM16LE bytes.

    Timing follows PARIS: a dot lasts 1.2 / WPM seconds.  Lowercase ASCII
    letters are accepted; unsupported characters raise ValueError.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    wpm = _real_parameter("wpm", wpm)
    tone_hz = _real_parameter("tone_hz", tone_hz)
    amplitude = _real_parameter("amplitude", amplitude)
    sample_rate = _validate_sample_rate(sample_rate)
    if wpm <= 0:
        raise ValueError("wpm must be positive")
    if tone_hz <= 0 or tone_hz >= sample_rate / 2:
        raise ValueError("tone_hz must be positive and below the Nyquist frequency")
    if not 0 <= amplitude <= 1:
        raise ValueError("amplitude must be between 0 and 1")

    for index, char in enumerate(text):
        if char != " " and char.upper() not in _MORSE:
            raise ValueError(f"unsupported Morse character {char!r} at index {index}")

    dot_seconds = 1.2 / wpm
    dot_samples = _segment_samples(dot_seconds, 1, sample_rate)
    dash_samples = _segment_samples(dot_seconds, 3, sample_rate)
    symbol_gap_samples = _segment_samples(dot_seconds, 1, sample_rate)
    character_gap_samples = _segment_samples(dot_seconds, 3, sample_rate)
    word_gap_samples = _segment_samples(dot_seconds, 7, sample_rate)

    # Each item is (sample count, keyed); spaces only separate actual words.
    segments = []
    for word in text.split(" "):
        if not word:
            continue
        if segments:
            segments.append((word_gap_samples, False))
        for char_index, char in enumerate(word):
            if char_index:
                segments.append((character_gap_samples, False))
            code = _MORSE[char.upper()]
            for symbol_index, symbol in enumerate(code):
                if symbol_index:
                    segments.append((symbol_gap_samples, False))
                duration = dot_samples if symbol == "." else dash_samples
                segments.append((duration, True))

    pcm = array.array("h")
    if pcm.itemsize != 2:
        raise RuntimeError("array('h') is not 16-bit on this platform")

    sample_index = 0
    phase_step = 2 * math.pi * tone_hz / sample_rate
    scale = 32767 * amplitude
    nominal_ramp = int(round(0.005 * sample_rate))

    for duration, keyed in segments:
        if not keyed:
            pcm.extend(array.array("h", [0]) * duration)
            sample_index += duration
            continue

        ramp = min(nominal_ramp, duration // 2)
        for offset in range(duration):
            envelope = 1.0
            edge_distance = min(offset, duration - 1 - offset)
            if edge_distance < ramp:
                envelope = 0.5 - 0.5 * math.cos(
                    math.pi * edge_distance / ramp
                )
            phase = phase_step * (sample_index + offset)
            pcm.append(int(round(scale * envelope * math.sin(phase))))
        sample_index += duration

    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()


def write_wav(path, pcm, sample_rate=48000):
    """Write mono PCM16LE bytes to a RIFF/WAVE file."""
    sample_rate = _validate_sample_rate(sample_rate)
    if not isinstance(pcm, (bytes, bytearray, memoryview)):
        raise TypeError("pcm must be bytes-like")
    pcm = bytes(pcm)
    if len(pcm) % 2:
        raise ValueError("pcm must contain complete 16-bit samples")

    # wave.open accepts file objects; decode path-like objects for portability.
    if isinstance(path, (bytes, os.PathLike)):
        path = os.fsdecode(path)
    with wave.open(path, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)
