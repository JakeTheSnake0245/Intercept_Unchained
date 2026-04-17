"""FMDemodConsumer — demodulates FM from CU8 IQ and pipes PCM to a decoder.

Performs FM (or AM/USB/LSB) demodulation in-process using numpy — the
same algorithm as the listening-post waterfall monitor.  The resulting
int16 PCM is written to the stdin of a configurable decoder subprocess
(e.g. direwolf for AX.25 AFSK or multimon-ng for GMSK/POCSAG).

Decoded lines from the subprocess stdout are forwarded to an optional
``on_decoded`` callback.
"""

from __future__ import annotations

import subprocess
import threading
from typing import Callable

import numpy as np

from utils.logging import get_logger
from utils.process import register_process, safe_terminate, unregister_process
from utils.waterfall_fft import cu8_to_complex

logger = get_logger('intercept.ground_station.fm_demod')

AUDIO_RATE = 48_000  # Hz — standard rate for direwolf / multimon-ng


class FMDemodConsumer:
    """CU8 IQ → FM demodulation → int16 PCM → decoder subprocess stdin."""

    def __init__(
        self,
        decoder_cmd: list[str],
        *,
        modulation: str = 'fm',
        on_decoded: Callable[[str], None] | None = None,
    ):
        """
        Args:
            decoder_cmd: Decoder command + args, e.g.
                ``['direwolf', '-r', '48000', '-']`` or
                ``['multimon-ng', '-t', 'raw', '-a', 'AFSK1200', '-']``.
            modulation: ``'fm'``, ``'am'``, ``'usb'``, ``'lsb'``.
            on_decoded: Callback invoked with each decoded line from stdout.
        """
        self._decoder_cmd = decoder_cmd
        self._modulation = modulation.lower()
        self._on_decoded = on_decoded
        self._proc: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._center_mhz = 0.0
        self._sample_rate = 0
        self._rotator_phase = 0.0

    # ------------------------------------------------------------------
    # IQConsumer protocol
    # ------------------------------------------------------------------

    def on_start(
        self,
        center_mhz: float,
        sample_rate: int,
        *,
        start_freq_mhz: float,
        end_freq_mhz: float,
    ) -> None:
        self._center_mhz = center_mhz
        self._sample_rate = sample_rate
        self._rotator_phase = 0.0
        self._start_proc()

    def on_chunk(self, raw: bytes) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        try:
            pcm, self._rotator_phase = _demodulate(
                raw,
                sample_rate=self._sample_rate,
                center_mhz=self._center_mhz,
                monitor_freq_mhz=self._center_mhz,  # decode on-center
                modulation=self._modulation,
                rotator_phase=self._rotator_phase,
            )
            if pcm and self._proc.stdin:
                self._proc.stdin.write(pcm)
                self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # decoder exited
        except Exception as e:
            logger.debug(f"FMDemodConsumer on_chunk error: {e}")

    def on_stop(self) -> None:
        if self._proc:
            safe_terminate(self._proc)
            unregister_process(self._proc)
            self._proc = None
        if self._stdout_thread and self._stdout_thread.is_alive():
            self._stdout_thread.join(timeout=2)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_proc(self) -> None:
        import shutil
        if not shutil.which(self._decoder_cmd[0]):
            logger.warning(
                f"FMDemodConsumer: decoder '{self._decoder_cmd[0]}' not found — disabled"
            )
            return
        try:
            self._proc = subprocess.Popen(
                self._decoder_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            register_process(self._proc)
            self._stdout_thread = threading.Thread(
                target=self._read_stdout, daemon=True, name='fm-demod-stdout'
            )
            self._stdout_thread.start()
        except Exception as e:
            logger.error(f"FMDemodConsumer: failed to start decoder: {e}")
            self._proc = None

    def _read_stdout(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                decoded = line.decode('utf-8', errors='replace').rstrip()
                if decoded and self._on_decoded:
                    try:
                        self._on_decoded(decoded)
                    except Exception as e:
                        logger.debug(f"FMDemodConsumer callback error: {e}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# In-process FM demodulation (mirrors waterfall_websocket._demodulate_monitor_audio)
# ---------------------------------------------------------------------------


def _demodulate(
    raw: bytes,
    sample_rate: int,
    center_mhz: float,
    monitor_freq_mhz: float,
    modulation: str,
    rotator_phase: float,
) -> tuple[bytes | None, float]:
    """Demodulate CU8 IQ to int16 PCM.

    Returns ``(pcm_bytes, next_rotator_phase)``.
    """
    if len(raw) < 32 or sample_rate <= 0:
        return None, float(rotator_phase)

    samples = cu8_to_complex(raw)
    fs = float(sample_rate)
    freq_offset_hz = (float(monitor_freq_mhz) - float(center_mhz)) * 1e6
    nyquist = fs * 0.5
    if abs(freq_offset_hz) > nyquist * 0.98:
        return None, float(rotator_phase)

    phase_inc = (2.0 * np.pi * freq_offset_hz) / fs
    n = np.arange(samples.size, dtype=np.float64)
    rotator = np.exp(-1j * (float(rotator_phase) + phase_inc * n)).astype(np.complex64)
    next_phase = float((float(rotator_phase) + phase_inc * samples.size) % (2.0 * np.pi))
    shifted = samples * rotator

    mod = modulation.lower().strip()
    target_bb = 48_000.0
    pre_decim = max(1, int(fs // target_bb))
    if pre_decim > 1:
        usable = (shifted.size // pre_decim) * pre_decim
        if usable < pre_decim:
            return None, next_phase
        shifted = shifted[:usable].reshape(-1, pre_decim).mean(axis=1)
    fs1 = fs / pre_decim

    if shifted.size < 16:
        return None, next_phase

    if mod == 'fm':
        audio = np.angle(shifted[1:] * np.conj(shifted[:-1])).astype(np.float32)
    elif mod == 'am':
        envelope = np.abs(shifted).astype(np.float32)
        audio = envelope - float(np.mean(envelope))
    elif mod == 'usb':
        audio = np.real(shifted).astype(np.float32)
    elif mod == 'lsb':
        audio = -np.real(shifted).astype(np.float32)
    else:
        audio = np.real(shifted).astype(np.float32)

    if audio.size < 8:
        return None, next_phase

    audio = audio - float(np.mean(audio))

    # Resample to AUDIO_RATE
    out_len = int(audio.size * AUDIO_RATE / fs1)
    if out_len < 32:
        return None, next_phase
    x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False, dtype=np.float32)
    x_new = np.linspace(0.0, 1.0, out_len, endpoint=False, dtype=np.float32)
    audio = np.interp(x_new, x_old, audio).astype(np.float32)

    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        audio = audio * min(20.0, 0.85 / peak)

    pcm = np.clip(audio, -1.0, 1.0)
    return (pcm * 32767.0).astype(np.int16).tobytes(), next_phase
