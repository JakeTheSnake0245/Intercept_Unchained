"""
DSC (Digital Selective Calling) utilities.

VHF DSC is a maritime distress and safety calling system operating on 156.525 MHz
(VHF Channel 70). It provides automated calling for distress, urgency, safety,
and routine communications per ITU-R M.493.
"""

from .constants import (
    CATEGORY_PRIORITY,
    DISTRESS_NATURE_CODES,
    FORMAT_CODES,
    MID_COUNTRY_MAP,
    TELECOMMAND_CODES,
)
from .parser import (
    get_country_from_mmsi,
    get_distress_nature_text,
    get_format_text,
    parse_dsc_message,
)

__all__ = [
    'FORMAT_CODES',
    'DISTRESS_NATURE_CODES',
    'TELECOMMAND_CODES',
    'CATEGORY_PRIORITY',
    'MID_COUNTRY_MAP',
    'parse_dsc_message',
    'get_country_from_mmsi',
    'get_distress_nature_text',
    'get_format_text',
]
