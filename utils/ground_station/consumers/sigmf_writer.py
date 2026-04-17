"""SigMFConsumer — wraps SigMFWriter as an IQ bus consumer."""

from __future__ import annotations

from utils.logging import get_logger
from utils.sigmf import SigMFMetadata, SigMFWriter

logger = get_logger('intercept.ground_station.sigmf_consumer')


class SigMFConsumer:
    """IQ consumer that records CU8 chunks to a SigMF file pair."""

    def __init__(
        self,
        metadata: SigMFMetadata,
        on_complete: callable | None = None,
    ):
        """
        Args:
            metadata: Pre-populated SigMF metadata (satellite info, freq, etc.)
            on_complete: Optional callback invoked with ``(meta_path, data_path)``
                when the recording is closed.
        """
        self._metadata = metadata
        self._on_complete = on_complete
        self._writer: SigMFWriter | None = None

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
        self._metadata.center_frequency_hz = center_mhz * 1e6
        self._metadata.sample_rate = sample_rate
        self._writer = SigMFWriter(self._metadata)
        try:
            self._writer.open()
        except Exception as e:
            logger.error(f"SigMFConsumer: failed to open writer: {e}")
            self._writer = None

    def on_chunk(self, raw: bytes) -> None:
        if self._writer is None:
            return
        ok = self._writer.write_chunk(raw)
        if not ok and self._writer.aborted:
            logger.warning("SigMFConsumer: recording aborted (disk full)")
            self._writer = None

    def on_stop(self) -> None:
        if self._writer is None:
            return
        result = self._writer.close()
        self._writer = None
        if result and self._on_complete:
            try:
                self._on_complete(*result)
            except Exception as e:
                logger.debug(f"SigMFConsumer on_complete error: {e}")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def bytes_written(self) -> int:
        return self._writer.bytes_written if self._writer else 0
