"""SigMF metadata and writer for IQ recordings.

Writes raw CU8 I/Q data to ``.sigmf-data`` files and companion
``.sigmf-meta`` JSON metadata files conforming to the SigMF spec v1.x.

Output directory: ``instance/ground_station/recordings/``
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.logging import get_logger

logger = get_logger('intercept.sigmf')

# Abort recording if less than this many bytes are free on the disk
DEFAULT_MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MB

OUTPUT_DIR = Path('instance/ground_station/recordings')


@dataclass
class SigMFMetadata:
    """SigMF metadata block.

    Covers the fields most relevant for ground-station recordings.  The
    ``global`` block is always written; an ``annotations`` list is built
    incrementally if callers add annotation events.
    """

    sample_rate: int
    center_frequency_hz: float
    datatype: str = 'cu8'           # unsigned 8-bit I/Q (rtlsdr native)
    description: str = ''
    author: str = 'INTERCEPT ground station'
    recorder: str = 'INTERCEPT'
    hw: str = ''
    norad_id: int = 0
    satellite_name: str = ''
    latitude: float = 0.0
    longitude: float = 0.0
    annotations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        global_block: dict[str, Any] = {
            'core:datatype': self.datatype,
            'core:sample_rate': self.sample_rate,
            'core:version': '1.0.0',
            'core:recorder': self.recorder,
        }
        if self.description:
            global_block['core:description'] = self.description
        if self.author:
            global_block['core:author'] = self.author
        if self.hw:
            global_block['core:hw'] = self.hw
        if self.latitude or self.longitude:
            global_block['core:geolocation'] = {
                'type': 'Point',
                'coordinates': [self.longitude, self.latitude],
            }

        captures = [
            {
                'core:sample_start': 0,
                'core:frequency': self.center_frequency_hz,
                'core:datetime': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            }
        ]

        return {
            'global': global_block,
            'captures': captures,
            'annotations': self.annotations,
        }


class SigMFWriter:
    """Streams raw CU8 IQ bytes to a SigMF recording pair."""

    def __init__(
        self,
        metadata: SigMFMetadata,
        output_dir: Path | str | None = None,
        stem: str | None = None,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    ):
        self._metadata = metadata
        self._output_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        self._stem = stem or _default_stem(metadata)
        self._min_free_bytes = min_free_bytes

        self._data_path: Path | None = None
        self._meta_path: Path | None = None
        self._data_file = None
        self._bytes_written = 0
        self._aborted = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Create output directory and open the data file for writing."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._data_path = self._output_dir / f'{self._stem}.sigmf-data'
        self._meta_path = self._output_dir / f'{self._stem}.sigmf-meta'
        self._data_file = open(self._data_path, 'wb')
        self._bytes_written = 0
        self._aborted = False
        logger.info(f"SigMFWriter opened: {self._data_path}")

    def write_chunk(self, raw: bytes) -> bool:
        """Write a chunk of raw CU8 bytes.

        Returns False (and sets ``aborted``) if disk space drops below
        the minimum threshold.
        """
        if self._aborted or self._data_file is None:
            return False

        # Check free space before writing
        try:
            usage = shutil.disk_usage(self._output_dir)
            if usage.free < self._min_free_bytes:
                logger.warning(
                    f"SigMF recording aborted — disk free "
                    f"({usage.free // (1024**2)} MB) below "
                    f"{self._min_free_bytes // (1024**2)} MB threshold"
                )
                self._aborted = True
                self._data_file.close()
                self._data_file = None
                return False
        except Exception:
            pass

        self._data_file.write(raw)
        self._bytes_written += len(raw)
        return True

    def close(self) -> tuple[Path, Path] | None:
        """Flush data, write .sigmf-meta, close file.

        Returns ``(meta_path, data_path)`` on success, *None* if never
        opened or already aborted before any data was written.
        """
        if self._data_file is not None:
            try:
                self._data_file.flush()
                self._data_file.close()
            except Exception:
                pass
            self._data_file = None

        if self._data_path is None or self._meta_path is None:
            return None
        if self._bytes_written == 0 and not self._aborted:
            # Nothing written — clean up empty file
            self._data_path.unlink(missing_ok=True)
            return None

        try:
            meta_dict = self._metadata.to_dict()
            self._meta_path.write_text(
                json.dumps(meta_dict, indent=2), encoding='utf-8'
            )
        except Exception as e:
            logger.error(f"Failed to write SigMF metadata: {e}")

        logger.info(
            f"SigMFWriter closed: {self._bytes_written} bytes → {self._data_path}"
        )
        return self._meta_path, self._data_path

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def data_path(self) -> Path | None:
        return self._data_path

    @property
    def meta_path(self) -> Path | None:
        return self._meta_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_stem(meta: SigMFMetadata) -> str:
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    sat = (meta.satellite_name or 'unknown').replace(' ', '_').replace('/', '-')
    freq_khz = int(meta.center_frequency_hz / 1000)
    return f'{ts}_{sat}_{freq_khz}kHz'
