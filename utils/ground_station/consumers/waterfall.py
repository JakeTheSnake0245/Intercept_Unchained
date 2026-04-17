"""WaterfallConsumer — converts CU8 IQ chunks into binary waterfall frames.

Frames are placed on an ``output_queue`` that the WebSocket endpoint
(``/ws/satellite_waterfall``) drains and sends to the browser.

Reuses :mod:`utils.waterfall_fft` for FFT processing so the wire format
is identical to the main listening-post waterfall.
"""

from __future__ import annotations

import queue
import time

from utils.logging import get_logger
from utils.waterfall_fft import (
    build_binary_frame,
    compute_power_spectrum,
    cu8_to_complex,
    quantize_to_uint8,
)

logger = get_logger('intercept.ground_station.waterfall_consumer')

FFT_SIZE = 1024
AVG_COUNT = 4
FPS = 20
DB_MIN: float | None = None  # auto-range
DB_MAX: float | None = None


class WaterfallConsumer:
    """IQ consumer that produces waterfall binary frames."""

    def __init__(
        self,
        output_queue: queue.Queue | None = None,
        fft_size: int = FFT_SIZE,
        avg_count: int = AVG_COUNT,
        fps: int = FPS,
        db_min: float | None = DB_MIN,
        db_max: float | None = DB_MAX,
    ):
        self.output_queue: queue.Queue = output_queue or queue.Queue(maxsize=120)
        self._fft_size = fft_size
        self._avg_count = avg_count
        self._fps = fps
        self._db_min = db_min
        self._db_max = db_max

        self._center_mhz = 0.0
        self._start_freq = 0.0
        self._end_freq = 0.0
        self._sample_rate = 0
        self._buffer = b''
        self._required_bytes = 0
        self._frame_interval = 1.0 / max(1, fps)
        self._last_frame_time = 0.0

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
        self._start_freq = start_freq_mhz
        self._end_freq = end_freq_mhz
        # How many IQ samples (pairs) we need for one FFT frame
        required_samples = max(
            self._fft_size * self._avg_count,
            sample_rate // max(1, self._fps),
        )
        self._required_bytes = required_samples * 2  # 1 byte I + 1 byte Q
        self._frame_interval = 1.0 / max(1, self._fps)
        self._buffer = b''
        self._last_frame_time = 0.0

    def on_chunk(self, raw: bytes) -> None:
        self._buffer += raw
        now = time.monotonic()
        if (now - self._last_frame_time) < self._frame_interval:
            return
        if len(self._buffer) < self._required_bytes:
            return

        chunk = self._buffer[-self._required_bytes:]
        self._buffer = b''
        self._last_frame_time = now

        try:
            samples = cu8_to_complex(chunk)
            power_db = compute_power_spectrum(
                samples, fft_size=self._fft_size, avg_count=self._avg_count
            )
            quantized = quantize_to_uint8(power_db, db_min=self._db_min, db_max=self._db_max)
            frame = build_binary_frame(self._start_freq, self._end_freq, quantized)
        except Exception as e:
            logger.debug(f"WaterfallConsumer FFT error: {e}")
            return

        # Non-blocking enqueue: drop oldest if full
        if self.output_queue.full():
            try:
                self.output_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.output_queue.put_nowait(frame)
        except queue.Full:
            pass

    def on_stop(self) -> None:
        self._buffer = b''
