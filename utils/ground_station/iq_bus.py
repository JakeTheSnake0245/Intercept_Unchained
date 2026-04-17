"""IQ broadcast bus — single SDR producer, multiple consumers.

The :class:`IQBus` claims an SDR device, spawns a capture subprocess
(``rx_sdr`` / ``rtl_sdr``), reads raw CU8 bytes from stdout in a
producer thread, and calls :meth:`IQConsumer.on_chunk` on every
registered consumer for each chunk.

Consumers are responsible for their own internal buffering.  The bus
does *not* block on slow consumers — each consumer's ``on_chunk`` is
called in the producer thread, so consumers must be non-blocking.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Protocol, runtime_checkable

from utils.logging import get_logger
from utils.process import register_process, safe_terminate, unregister_process

logger = get_logger('intercept.ground_station.iq_bus')

CHUNK_SIZE = 65_536  # bytes per read (~27 ms @ 2.4 Msps CU8)


@runtime_checkable
class IQConsumer(Protocol):
    """Protocol for objects that receive raw CU8 chunks from the IQ bus."""

    def on_chunk(self, raw: bytes) -> None:
        """Called with each raw CU8 chunk from the SDR.  Must be fast."""
        ...

    def on_start(
        self,
        center_mhz: float,
        sample_rate: int,
        *,
        start_freq_mhz: float,
        end_freq_mhz: float,
    ) -> None:
        """Called once when the bus starts, before the first chunk."""
        ...

    def on_stop(self) -> None:
        """Called once when the bus stops (LOS or manual stop)."""
        ...


class _NoopConsumer:
    """Fallback used internally for isinstance checks."""

    def on_chunk(self, raw: bytes) -> None:
        pass

    def on_start(self, center_mhz, sample_rate, *, start_freq_mhz, end_freq_mhz):
        pass

    def on_stop(self) -> None:
        pass


class IQBus:
    """Single-SDR IQ capture bus with fan-out to multiple consumers."""

    def __init__(
        self,
        *,
        center_mhz: float,
        sample_rate: int = 2_400_000,
        gain: float | None = None,
        device_index: int = 0,
        sdr_type: str = 'rtlsdr',
        ppm: int | None = None,
        bias_t: bool = False,
    ):
        self._center_mhz = center_mhz
        self._sample_rate = sample_rate
        self._gain = gain
        self._device_index = device_index
        self._sdr_type = sdr_type
        self._ppm = ppm
        self._bias_t = bias_t

        self._consumers: list[IQConsumer] = []
        self._consumers_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._producer_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self._current_freq_mhz = center_mhz

    # ------------------------------------------------------------------
    # Consumer management
    # ------------------------------------------------------------------

    def add_consumer(self, consumer: IQConsumer) -> None:
        with self._consumers_lock:
            if consumer not in self._consumers:
                self._consumers.append(consumer)

    def remove_consumer(self, consumer: IQConsumer) -> None:
        with self._consumers_lock:
            self._consumers = [c for c in self._consumers if c is not consumer]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> tuple[bool, str]:
        """Start IQ capture.  Returns (success, error_message)."""
        if self._running:
            return True, ''

        try:
            cmd = self._build_command(self._center_mhz)
        except Exception as e:
            return False, f'Failed to build IQ capture command: {e}'

        if not shutil.which(cmd[0]):
            return False, f'Required tool "{cmd[0]}" not found. Install SoapySDR (rx_sdr) or rtl-sdr.'

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            register_process(self._proc)
        except Exception as e:
            return False, f'Failed to spawn IQ capture: {e}'

        # Brief check that the process actually started
        time.sleep(0.3)
        if self._proc.poll() is not None:
            stderr_out = ''
            if self._proc.stderr:
                try:
                    stderr_out = self._proc.stderr.read().decode('utf-8', errors='replace').strip()
                except Exception:
                    pass
            unregister_process(self._proc)
            self._proc = None
            detail = f': {stderr_out}' if stderr_out else ''
            return False, f'IQ capture process exited immediately{detail}'

        self._stop_event.clear()
        self._running = True

        span_mhz = self._sample_rate / 1e6
        start_freq_mhz = self._center_mhz - span_mhz / 2
        end_freq_mhz = self._center_mhz + span_mhz / 2

        with self._consumers_lock:
            for consumer in list(self._consumers):
                try:
                    consumer.on_start(
                        self._center_mhz,
                        self._sample_rate,
                        start_freq_mhz=start_freq_mhz,
                        end_freq_mhz=end_freq_mhz,
                    )
                except Exception as e:
                    logger.warning(f"Consumer on_start error: {e}")

        self._producer_thread = threading.Thread(
            target=self._producer_loop, daemon=True, name='iq-bus-producer'
        )
        self._producer_thread.start()
        logger.info(
            f"IQBus started: {self._center_mhz} MHz, sr={self._sample_rate}, "
            f"device={self._sdr_type}:{self._device_index}"
        )
        return True, ''

    def stop(self) -> None:
        """Stop IQ capture and notify all consumers."""
        self._stop_event.set()
        if self._proc:
            safe_terminate(self._proc)
            unregister_process(self._proc)
            self._proc = None
        if self._producer_thread and self._producer_thread.is_alive():
            self._producer_thread.join(timeout=3)
        self._running = False

        with self._consumers_lock:
            for consumer in list(self._consumers):
                try:
                    consumer.on_stop()
                except Exception as e:
                    logger.warning(f"Consumer on_stop error: {e}")

        logger.info("IQBus stopped")

    def retune(self, new_freq_mhz: float) -> tuple[bool, str]:
        """Retune by stopping and restarting the capture process."""
        self._current_freq_mhz = new_freq_mhz
        if not self._running:
            return False, 'Not running'

        # Stop the current process
        self._stop_event.set()
        if self._proc:
            safe_terminate(self._proc)
            unregister_process(self._proc)
            self._proc = None
        if self._producer_thread and self._producer_thread.is_alive():
            self._producer_thread.join(timeout=2)

        # Restart at new frequency
        self._stop_event.clear()
        try:
            cmd = self._build_command(new_freq_mhz)
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            register_process(self._proc)
        except Exception as e:
            self._running = False
            return False, f'Retune failed: {e}'

        self._producer_thread = threading.Thread(
            target=self._producer_loop, daemon=True, name='iq-bus-producer'
        )
        self._producer_thread.start()
        logger.info(f"IQBus retuned to {new_freq_mhz:.6f} MHz")
        return True, ''

    @property
    def running(self) -> bool:
        return self._running

    @property
    def center_mhz(self) -> float:
        return self._current_freq_mhz

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _producer_loop(self) -> None:
        """Read CU8 chunks from the subprocess and fan out to consumers."""
        assert self._proc is not None
        assert self._proc.stdout is not None

        try:
            while not self._stop_event.is_set():
                if self._proc.poll() is not None:
                    logger.warning("IQBus: capture process exited unexpectedly")
                    break
                raw = self._proc.stdout.read(CHUNK_SIZE)
                if not raw:
                    break
                with self._consumers_lock:
                    consumers = list(self._consumers)
                for consumer in consumers:
                    try:
                        consumer.on_chunk(raw)
                    except Exception as e:
                        logger.warning(f"Consumer on_chunk error: {e}")
        except Exception as e:
            logger.error(f"IQBus producer loop error: {e}")

    def _build_command(self, freq_mhz: float) -> list[str]:
        """Build the IQ capture command using the SDR factory."""
        from utils.sdr import SDRFactory, SDRType
        from utils.sdr.base import SDRDevice

        type_map = {
            'rtlsdr': SDRType.RTL_SDR,
            'rtl_sdr': SDRType.RTL_SDR,
            'hackrf': SDRType.HACKRF,
            'limesdr': SDRType.LIME_SDR,
            'airspy': SDRType.AIRSPY,
            'sdrplay': SDRType.SDRPLAY,
        }
        sdr_type = type_map.get(self._sdr_type.lower(), SDRType.RTL_SDR)
        builder = SDRFactory.get_builder(sdr_type)
        caps = builder.get_capabilities()
        device = SDRDevice(
            sdr_type=sdr_type,
            index=self._device_index,
            name=f'{sdr_type.value}-{self._device_index}',
            serial='N/A',
            driver=sdr_type.value,
            capabilities=caps,
        )
        return builder.build_iq_capture_command(
            device=device,
            frequency_mhz=freq_mhz,
            sample_rate=self._sample_rate,
            gain=self._gain,
            ppm=self._ppm,
            bias_t=self._bias_t,
        )
