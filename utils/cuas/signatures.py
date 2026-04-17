"""Known drone RF/WiFi/BT signatures for C-UAS detection."""

# WiFi SSID prefixes that indicate drone hardware
WIFI_DRONE_PATTERNS: list[str] = [
    "DJI-", "DJI_", "Mavic_", "MAVIC-", "PHANTOM-", "Phantom_",
    "Spark-", "SPARK-", "TELLO-", "Tello-", "MATRICE-", "Matrice_",
    "INSPIRE-", "AIR2S-", "AIR2-", "MINI2-", "MINI3-", "MINI4-",
    "AVATA-", "RC-N1", "SKYDIO-", "Skydio-",
    "Autel-EVO-", "AUTEL-", "Parrot-", "ANAFI-", "FIMI-",
    "TinyHawk-", "BETAFPV-", "iFlight-", "EACHINE-",
    "Hubsan-", "XIAOMI-DRONE-", "YUNEEC-", "TYPHOON-",
    "FPV_", "DRONE_", "UAV_", "WIFI_FPV", "FreedCam-",
    "TP-LINK_DRONE", "QUADCOPTER-",
]

# Bluetooth device name fragments indicating drone hardware
BT_DRONE_PATTERNS: list[str] = [
    "DJI", "Mavic", "Phantom", "Spark", "Tello", "Mini 2", "Mini 3",
    "Mini 4", "Mini 3 Pro", "Air 2", "Air 2S", "Matrice", "Inspire",
    "Avata", "RC Pro", "RC-N1", "RC-N2",
    "Parrot", "Anafi", "Skydio", "Autel", "EVO", "XBlade", "Ryze",
    "BETAFPV", "iFlight", "Eachine", "Hubsan",
]

# Known drone manufacturer OUIs (MAC prefix -> make)
DRONE_OUI_MAP: dict[str, str] = {
    "60:60:1F": "DJI",
    "A0:B1:C4": "DJI",
    "48:1C:B9": "DJI",
    "34:D2:62": "DJI",
    "B4:B0:24": "DJI",
    "AC:23:3F": "DJI",
    "28:F5:37": "DJI",
    "48:79:E7": "DJI",
    "90:03:B7": "Parrot",
    "48:1C:B9": "Parrot",
    "00:12:1C": "Skydio",
    "D0:EF:C1": "Autel",
    "CC:1B:E0": "Autel",
    "C4:7F:51": "Yuneec",
}

# UAS control/telemetry frequency bands (MHz)
UAS_BANDS: list[dict] = [
    {"name": "UAS_RC_433",       "start_mhz": 433.050, "stop_mhz": 434.790, "step_khz": 2},
    {"name": "UAS_TELEMETRY_868","start_mhz": 868.0,   "stop_mhz": 868.6,   "step_khz": 2},
    {"name": "UAS_ISM_900",      "start_mhz": 902.0,   "stop_mhz": 928.0,   "step_khz": 5},
    {"name": "UAS_CONTROL_24G",  "start_mhz": 2400.0,  "stop_mhz": 2483.5,  "step_khz": 100},
    {"name": "UAS_VIDEO_58G",    "start_mhz": 5725.0,  "stop_mhz": 5850.0,  "step_khz": 200},
    {"name": "GPS_L1",           "start_mhz": 1574.0,  "stop_mhz": 1577.0,  "step_khz": 1},
]

# Standard 5.8 GHz FPV channel center frequencies (MHz)
FPV_CHANNELS: list[dict] = [
    # Band A
    {"band": "A", "ch": 1, "freq_mhz": 5865}, {"band": "A", "ch": 2, "freq_mhz": 5845},
    {"band": "A", "ch": 3, "freq_mhz": 5825}, {"band": "A", "ch": 4, "freq_mhz": 5805},
    {"band": "A", "ch": 5, "freq_mhz": 5785}, {"band": "A", "ch": 6, "freq_mhz": 5765},
    {"band": "A", "ch": 7, "freq_mhz": 5745}, {"band": "A", "ch": 8, "freq_mhz": 5725},
    # Band B
    {"band": "B", "ch": 1, "freq_mhz": 5733}, {"band": "B", "ch": 2, "freq_mhz": 5752},
    {"band": "B", "ch": 3, "freq_mhz": 5771}, {"band": "B", "ch": 4, "freq_mhz": 5790},
    {"band": "B", "ch": 5, "freq_mhz": 5809}, {"band": "B", "ch": 6, "freq_mhz": 5828},
    {"band": "B", "ch": 7, "freq_mhz": 5847}, {"band": "B", "ch": 8, "freq_mhz": 5866},
    # Band E
    {"band": "E", "ch": 1, "freq_mhz": 5705}, {"band": "E", "ch": 2, "freq_mhz": 5685},
    {"band": "E", "ch": 3, "freq_mhz": 5665}, {"band": "E", "ch": 4, "freq_mhz": 5645},
    {"band": "E", "ch": 5, "freq_mhz": 5885}, {"band": "E", "ch": 6, "freq_mhz": 5905},
    {"band": "E", "ch": 7, "freq_mhz": 5925}, {"band": "E", "ch": 8, "freq_mhz": 5945},
    # Band F / Airwave
    {"band": "F", "ch": 1, "freq_mhz": 5740}, {"band": "F", "ch": 2, "freq_mhz": 5760},
    {"band": "F", "ch": 3, "freq_mhz": 5780}, {"band": "F", "ch": 4, "freq_mhz": 5800},
    {"band": "F", "ch": 5, "freq_mhz": 5820}, {"band": "F", "ch": 6, "freq_mhz": 5840},
    {"band": "F", "ch": 7, "freq_mhz": 5860}, {"band": "F", "ch": 8, "freq_mhz": 5880},
    # Band R (Raceband)
    {"band": "R", "ch": 1, "freq_mhz": 5658}, {"band": "R", "ch": 2, "freq_mhz": 5695},
    {"band": "R", "ch": 3, "freq_mhz": 5732}, {"band": "R", "ch": 4, "freq_mhz": 5769},
    {"band": "R", "ch": 5, "freq_mhz": 5806}, {"band": "R", "ch": 6, "freq_mhz": 5843},
    {"band": "R", "ch": 7, "freq_mhz": 5880}, {"band": "R", "ch": 8, "freq_mhz": 5917},
]

# Bluetooth manufacturer IDs associated with drones
BT_MANUFACTURER_IDS: dict[int, str] = {
    0x0C03: "DJI",
    0x0075: "Parrot",
}

# BLE Remote ID service UUID (ASTM F3411-22a §5.4.4)
REMOTE_ID_SERVICE_UUID = "0000FFFA-0000-1000-8000-00805F9B34FB"
