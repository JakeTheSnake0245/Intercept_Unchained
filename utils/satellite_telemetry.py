"""Satellite telemetry packet parsers.

Provides pure-Python decoders for common amateur/CubeSat protocols:
- AX.25 (callsign-addressed frames)
- CSP (CubeSat Space Protocol)
- CCSDS TM (space packet primary header)

Also provides a PayloadAnalyzer that generates multi-interpretation
views of raw binary data (hex dump, float32, uint16/32, strings).
"""

from __future__ import annotations

import math
import string
import struct
from datetime import datetime

# ---------------------------------------------------------------------------
# AX.25 parser
# ---------------------------------------------------------------------------


def _decode_ax25_callsign(addr_bytes: bytes) -> str:
    """Decode a 7-byte AX.25 address field into a 'CALL-SSID' string.

    The first 6 bytes encode the callsign (each ASCII character left-shifted
    by 1 bit).  The 7th byte encodes the SSID in bits 4-1.

    Args:
        addr_bytes: Exactly 7 bytes of raw address data.

    Returns:
        A callsign string such as ``"N0CALL-3"`` or ``"N0CALL"`` (no suffix
        when SSID is 0).
    """
    callsign = "".join(chr(b >> 1) for b in addr_bytes[:6]).rstrip()
    ssid = (addr_bytes[6] >> 1) & 0x0F
    return f"{callsign}-{ssid}" if ssid else callsign


def parse_ax25(data: bytes) -> dict | None:
    """Parse an AX.25 frame from raw bytes.

    Decodes destination and source callsigns, optional repeater addresses,
    control byte, optional PID byte, and payload.

    Args:
        data: Raw bytes of the AX.25 frame (without HDLC flags or FCS).

    Returns:
        A dict with parsed fields or ``None`` if the frame is too short or
        cannot be decoded.
    """
    try:
        # Minimum: 7 (dest) + 7 (src) + 1 (control) = 15 bytes
        if len(data) < 15:
            return None

        destination = _decode_ax25_callsign(data[0:7])
        source = _decode_ax25_callsign(data[7:14])

        # Walk repeater addresses.  The H-bit (LSB of byte 6 in each address)
        # being set means this is the last address in the chain.
        offset = 14  # byte index of the last byte in the source field
        repeaters: list[str] = []

        if not (data[offset] & 0x01):
            # More addresses follow; read up to 8 repeaters.
            for _ in range(8):
                rep_start = offset + 1
                rep_end = rep_start + 7
                if rep_end > len(data):
                    break
                repeaters.append(_decode_ax25_callsign(data[rep_start:rep_end]))
                offset = rep_end - 1  # last byte of this repeater field
                if data[offset] & 0x01:
                    # H-bit set — this was the final address
                    break

        # Control byte follows the last address field
        ctrl_offset = offset + 1
        if ctrl_offset >= len(data):
            return None

        control = data[ctrl_offset]
        payload_offset = ctrl_offset + 1

        # PID byte is present for I-frames (bits 0-1 == 0b00) and
        # UI-frames (bits 0-5 == 0b000011).  More generally: absent only
        # for pure unnumbered frames where (control & 0x03) == 0x03 AND
        # control is not 0x03 itself (UI).
        pid: int | None = None
        is_unnumbered = (control & 0x03) == 0x03
        is_ui = control == 0x03

        if not is_unnumbered or is_ui:
            if payload_offset < len(data):
                pid = data[payload_offset]
                payload_offset += 1

        payload = data[payload_offset:]

        return {
            "protocol": "AX.25",
            "destination": destination,
            "source": source,
            "repeaters": repeaters,
            "control": control,
            "pid": pid,
            "payload": payload,
            "payload_hex": payload.hex(),
            "payload_length": len(payload),
        }

    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# CSP parser
# ---------------------------------------------------------------------------


def parse_csp(data: bytes) -> dict | None:
    """Parse a CSP v1 (CubeSat Space Protocol) header.

    The first 4 bytes form a big-endian 32-bit header word with the
    following bit layout::

        bits 31-27  priority     (5 bits)
        bits 26-22  source       (5 bits)
        bits 21-17  destination  (5 bits)
        bits 16-12  dest_port    (5 bits)
        bits 11-6   src_port     (6 bits)
        bits  5-0   flags        (6 bits)

    Args:
        data: Raw bytes starting from the CSP header.

    Returns:
        A dict with parsed CSP fields and payload, or ``None`` on failure.
    """
    try:
        if len(data) < 4:
            return None

        header: int = struct.unpack(">I", data[:4])[0]

        priority    = (header >> 27) & 0x1F
        source      = (header >> 22) & 0x1F
        destination = (header >> 17) & 0x1F
        dest_port   = (header >> 12) & 0x1F
        src_port    = (header >> 6)  & 0x3F
        raw_flags   = header & 0x3F

        flags = {
            "frag": bool(raw_flags & 0x10),
            "hmac": bool(raw_flags & 0x08),
            "xtea": bool(raw_flags & 0x04),
            "rdp":  bool(raw_flags & 0x02),
            "crc":  bool(raw_flags & 0x01),
        }

        payload = data[4:]

        return {
            "protocol":     "CSP",
            "priority":     priority,
            "source":       source,
            "destination":  destination,
            "dest_port":    dest_port,
            "src_port":     src_port,
            "flags":        flags,
            "payload":      payload,
            "payload_hex":  payload.hex(),
            "payload_length": len(payload),
        }

    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# CCSDS parser
# ---------------------------------------------------------------------------


def parse_ccsds(data: bytes) -> dict | None:
    """Parse a CCSDS Space Packet primary header (6 bytes).

    Header layout::

        bytes 0-1:  version (3 bits) | packet_type (1 bit) |
                    secondary_header_flag (1 bit) | APID (11 bits)
        bytes 2-3:  sequence_flags (2 bits) | sequence_count (14 bits)
        bytes 4-5:  data_length field (16 bits, = actual_payload_length - 1)

    Args:
        data: Raw bytes starting from the CCSDS primary header.

    Returns:
        A dict with parsed CCSDS fields and payload, or ``None`` on failure.
    """
    try:
        if len(data) < 6:
            return None

        word0: int = struct.unpack(">H", data[0:2])[0]
        word1: int = struct.unpack(">H", data[2:4])[0]
        word2: int = struct.unpack(">H", data[4:6])[0]

        version                = (word0 >> 13) & 0x07
        packet_type            = (word0 >> 12) & 0x01
        secondary_header_flag  = bool((word0 >> 11) & 0x01)
        apid                   = word0 & 0x07FF

        sequence_flags  = (word1 >> 14) & 0x03
        sequence_count  = word1 & 0x3FFF

        data_length = word2  # raw field; actual user data bytes = data_length + 1

        payload = data[6:]

        return {
            "protocol":             "CCSDS_TM",
            "version":              version,
            "packet_type":          packet_type,
            "secondary_header":     secondary_header_flag,
            "apid":                 apid,
            "sequence_flags":       sequence_flags,
            "sequence_count":       sequence_count,
            "data_length":          data_length,
            "payload":              payload,
            "payload_hex":          payload.hex(),
            "payload_length":       len(payload),
        }

    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Payload analyzer
# ---------------------------------------------------------------------------

_PRINTABLE = set(string.printable) - set("\t\n\r\x0b\x0c")


def _hex_dump(data: bytes) -> str:
    """Format bytes as an annotated hex dump, 16 bytes per line.

    Each line is formatted as::

        OOOO: XX XX XX XX  XX XX XX XX  XX XX XX XX  XX XX XX XX  ASCII

    where ``OOOO`` is the hex offset and ``ASCII`` shows printable characters
    (non-printable replaced with ``'.'``).

    Args:
        data: Bytes to format.

    Returns:
        Multi-line hex dump string (trailing newline on each line).
    """
    lines: list[str] = []
    for row in range(0, len(data), 16):
        chunk = data[row : row + 16]
        # Build groups of 4 bytes separated by two spaces
        groups: list[str] = []
        for g in range(0, 16, 4):
            group_bytes = chunk[g : g + 4]
            groups.append(" ".join(f"{b:02X}" for b in group_bytes))
        hex_part = "  ".join(groups)
        # Pad to fixed width: 16 bytes × 3 chars - 1 space + 3 group separators
        # Maximum width: 11+2+11+2+11+2+11 = 50 chars; pad to 50
        hex_part = hex_part.ljust(50)
        ascii_part = "".join(chr(b) if chr(b) in _PRINTABLE else "." for b in chunk)
        lines.append(f"{row:04X}: {hex_part}  {ascii_part}\n")
    return "".join(lines)


def _extract_strings(data: bytes, min_len: int = 3) -> list[str]:
    """Extract runs of printable ASCII characters of at least ``min_len``."""
    results: list[str] = []
    current: list[str] = []
    for b in data:
        ch = chr(b)
        if ch in _PRINTABLE:
            current.append(ch)
        else:
            if len(current) >= min_len:
                results.append("".join(current))
            current = []
    if len(current) >= min_len:
        results.append("".join(current))
    return results


def analyze_payload(data: bytes) -> dict:
    """Generate a multi-interpretation analysis of raw bytes.

    Produces a hex dump, several numeric/string interpretations, and a
    list of heuristic observations about plausible sensor values.

    Args:
        data: Raw bytes to analyze.

    Returns:
        A dict containing ``hex_dump``, ``length``, ``interpretations``,
        and ``heuristics`` keys.  Never raises an exception.
    """
    try:
        hex_dump = _hex_dump(data)
        length = len(data)

        # --- float32 (little-endian) ---
        float32_values: list[float] = []
        for i in range(0, length - 3, 4):
            (val,) = struct.unpack_from("<f", data, i)
            if not math.isnan(val) and abs(val) <= 1e9:
                float32_values.append(val)

        # --- uint16 little-endian ---
        uint16_values: list[int] = []
        for i in range(0, length - 1, 2):
            (val,) = struct.unpack_from("<H", data, i)
            uint16_values.append(val)

        # --- uint32 little-endian ---
        uint32_values: list[int] = []
        for i in range(0, length - 3, 4):
            (val,) = struct.unpack_from("<I", data, i)
            uint32_values.append(val)

        # --- printable string runs ---
        strings = _extract_strings(data, min_len=3)

        interpretations = {
            "float32":    float32_values,
            "uint16_le":  uint16_values,
            "uint32_le":  uint32_values,
            "strings":    strings,
        }

        # --- heuristics ---
        heuristics: list[str] = []
        used_as_voltage: set[int] = set()

        for idx, v in enumerate(float32_values):
            # Voltage: small positive float
            if 0.0 < v < 10.0:
                heuristics.append(f"Possible voltage: {v:.3f} V (index {idx})")
                used_as_voltage.add(idx)

        for idx, v in enumerate(float32_values):
            # Temperature: plausible range, not already flagged as voltage, not zero
            if -50.0 < v < 120.0 and idx not in used_as_voltage and v != 0.0:
                heuristics.append(f"Possible temperature: {v:.1f}°C (index {idx})")

        for idx, v in enumerate(float32_values):
            # Current: small positive float not already flagged as voltage
            if 0.0 < v < 5.0 and idx not in used_as_voltage:
                heuristics.append(f"Possible current: {v:.3f} A (index {idx})")

        for idx, v in enumerate(float32_values):
            # Unix timestamp: plausible range (roughly 2001–2033)
            if 1_000_000_000.0 < v < 2_000_000_000.0:
                ts = datetime.utcfromtimestamp(v)
                heuristics.append(f"Possible Unix timestamp: {ts} (index {idx})")

        return {
            "hex_dump":        hex_dump,
            "length":          length,
            "interpretations": interpretations,
            "heuristics":      heuristics,
        }

    except Exception:  # noqa: BLE001
        # Guarantee a safe return even on completely malformed input
        return {
            "hex_dump":        "",
            "length":          len(data) if isinstance(data, (bytes, bytearray)) else 0,
            "interpretations": {"float32": [], "uint16_le": [], "uint32_le": [], "strings": []},
            "heuristics":      [],
        }


# ---------------------------------------------------------------------------
# Auto-parser
# ---------------------------------------------------------------------------


def auto_parse(data: bytes) -> dict:
    """Attempt to decode a packet using each supported protocol in turn.

    Tries parsers in priority order: CSP → CCSDS → AX.25.  Returns the
    first successful parse merged with a ``payload_analysis`` key produced
    by :func:`analyze_payload`.

    Args:
        data: Raw bytes of the packet.

    Returns:
        A dict with parsed protocol fields plus ``payload_analysis``, or a
        fallback dict with ``protocol: 'unknown'`` and a top-level
        ``analysis`` key if no parser succeeds.
    """
    # CSP: 4-byte header minimum
    if len(data) >= 4:
        result = parse_csp(data)
        if result is not None:
            result["payload_analysis"] = analyze_payload(result["payload"])
            return result

    # CCSDS: 6-byte header minimum
    if len(data) >= 6:
        result = parse_ccsds(data)
        if result is not None:
            result["payload_analysis"] = analyze_payload(result["payload"])
            return result

    # AX.25: 15-byte frame minimum
    if len(data) >= 15:
        result = parse_ax25(data)
        if result is not None:
            result["payload_analysis"] = analyze_payload(result["payload"])
            return result

    # Nothing matched — return a raw analysis
    return {
        "protocol": "unknown",
        "raw_hex":  data.hex(),
        "analysis": analyze_payload(data),
    }
