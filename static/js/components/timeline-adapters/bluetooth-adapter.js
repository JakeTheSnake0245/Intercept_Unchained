/**
 * Bluetooth Timeline Adapter
 * Normalizes Bluetooth device data for the Activity Timeline component
 * Used by: Bluetooth mode, TSCM (Bluetooth detections)
 */

const BluetoothTimelineAdapter = (function() {
    'use strict';

    /**
     * RSSI to strength category mapping for Bluetooth
     * Bluetooth RSSI typically ranges from -30 (very close) to -100 (far)
     */
    const RSSI_THRESHOLDS = {
        VERY_STRONG: -45,  // 5 - device likely within 1m
        STRONG: -60,       // 4 - device likely within 3m
        MODERATE: -75,     // 3 - device likely within 10m
        WEAK: -90,         // 2 - device at edge of range
        MINIMAL: -100      // 1 - barely detectable
    };

    /**
     * Known device type patterns
     */
    const DEVICE_PATTERNS = {
        // Apple devices
        AIRPODS: /airpods/i,
        IPHONE: /iphone/i,
        IPAD: /ipad/i,
        MACBOOK: /macbook|mac\s*pro|imac/i,
        APPLE_WATCH: /apple\s*watch/i,
        AIRTAG: /airtag/i,

        // Trackers
        TILE: /tile/i,
        CHIPOLO: /chipolo/i,
        SAMSUNG_TAG: /smarttag|galaxy\s*tag/i,

        // Audio
        HEADPHONES: /headphone|earphone|earbud|bose|sony|beats|jabra|sennheiser/i,
        SPEAKER: /speaker|soundbar|echo|homepod|sonos/i,

        // Wearables
        FITBIT: /fitbit/i,
        GARMIN: /garmin/i,
        SMARTWATCH: /watch|band|mi\s*band|galaxy\s*fit/i,

        // Input devices
        KEYBOARD: /keyboard/i,
        MOUSE: /mouse|trackpad|magic/i,
        CONTROLLER: /controller|gamepad|xbox|playstation|dualshock/i,

        // Vehicles
        CAR: /car\s*kit|handsfree|obd|vehicle|toyota|honda|ford|bmw|mercedes/i
    };

    /**
     * Convert RSSI to strength category
     */
    function rssiToStrength(rssi) {
        if (rssi === null || rssi === undefined) return 3;

        const r = parseFloat(rssi);
        if (isNaN(r)) return 3;

        if (r > RSSI_THRESHOLDS.VERY_STRONG) return 5;
        if (r > RSSI_THRESHOLDS.STRONG) return 4;
        if (r > RSSI_THRESHOLDS.MODERATE) return 3;
        if (r > RSSI_THRESHOLDS.WEAK) return 2;
        return 1;
    }

    /**
     * Classify device type from name
     */
    function classifyDevice(name) {
        if (!name) return { type: 'unknown', category: 'device' };

        for (const [pattern, regex] of Object.entries(DEVICE_PATTERNS)) {
            if (regex.test(name)) {
                return {
                    type: pattern.toLowerCase(),
                    category: getCategoryForType(pattern)
                };
            }
        }

        return { type: 'unknown', category: 'device' };
    }

    /**
     * Get category for device type
     */
    function getCategoryForType(type) {
        const categories = {
            AIRPODS: 'audio',
            IPHONE: 'phone',
            IPAD: 'tablet',
            MACBOOK: 'computer',
            APPLE_WATCH: 'wearable',
            AIRTAG: 'tracker',
            TILE: 'tracker',
            CHIPOLO: 'tracker',
            SAMSUNG_TAG: 'tracker',
            HEADPHONES: 'audio',
            SPEAKER: 'audio',
            FITBIT: 'wearable',
            GARMIN: 'wearable',
            SMARTWATCH: 'wearable',
            KEYBOARD: 'input',
            MOUSE: 'input',
            CONTROLLER: 'input',
            CAR: 'vehicle'
        };
        return categories[type] || 'device';
    }

    /**
     * Format MAC address for display (truncated)
     */
    function formatMac(mac, full = false) {
        if (!mac) return 'Unknown';
        if (full) return mac.toUpperCase();
        return mac.substring(0, 8).toUpperCase() + '...';
    }

    /**
     * Determine if device is a tracker type
     */
    function isTracker(device) {
        if (device.is_tracker) return true;

        const name = device.name || '';
        return /airtag|tile|chipolo|smarttag|tracker/i.test(name);
    }

    /**
     * Normalize a Bluetooth device detection for the timeline
     */
    function normalizeDevice(device) {
        const mac = device.mac || device.address || device.id;
        const name = device.name || device.device_name || formatMac(mac);
        const classification = classifyDevice(name);

        const tags = [device.type || 'ble'];
        tags.push(classification.category);

        if (isTracker(device)) tags.push('tracker');
        if (device.is_beacon) tags.push('beacon');
        if (device.is_connectable) tags.push('connectable');
        if (device.manufacturer) tags.push('identified');

        return {
            id: mac,
            label: name,
            strength: rssiToStrength(device.rssi),
            duration: device.scan_duration || device.duration || 1000,
            type: classification.type,
            tags: tags,
            metadata: {
                mac: mac,
                rssi: device.rssi,
                device_type: device.type,
                manufacturer: device.manufacturer,
                services: device.services,
                is_tracker: isTracker(device),
                classification: classification
            }
        };
    }

    /**
     * Normalize for TSCM context (includes threat assessment)
     */
    function normalizeTscmDevice(device) {
        const normalized = normalizeDevice(device);

        // Add TSCM-specific tags
        if (device.is_new) normalized.tags.push('new');
        if (device.threat_level) normalized.tags.push(`threat-${device.threat_level}`);
        if (device.baseline_known === false) normalized.tags.push('unknown');

        normalized.metadata.threat_level = device.threat_level;
        normalized.metadata.first_seen = device.first_seen;
        normalized.metadata.appearance_count = device.appearance_count;

        return normalized;
    }

    /**
     * Batch normalize multiple devices
     */
    function normalizeDevices(devices, context = 'scan') {
        const normalizer = context === 'tscm' ? normalizeTscmDevice : normalizeDevice;
        return devices.map(normalizer);
    }

    /**
     * Create timeline configuration for Bluetooth mode
     */
    function getBluetoothConfig() {
        return {
            title: 'Device Activity',
            mode: 'bluetooth',
            visualMode: 'enriched',
            collapsed: false,
            showAnnotations: true,
            showLegend: true,
            defaultWindow: '15m',
            availableWindows: ['5m', '15m', '30m', '1h'],
            filters: {
                hideBaseline: { enabled: true, label: 'Hide Known', default: false },
                showOnlyNew: { enabled: true, label: 'New Only', default: false },
                showOnlyBurst: { enabled: false, label: 'Bursts', default: false }
            },
            customFilters: [
                {
                    key: 'showOnlyTrackers',
                    label: 'Trackers Only',
                    default: false,
                    predicate: (item) => item.tags.includes('tracker')
                },
                {
                    key: 'hideWearables',
                    label: 'Hide Wearables',
                    default: false,
                    predicate: (item) => !item.tags.includes('wearable')
                }
            ],
            maxItems: 75,
            maxDisplayedLanes: 12,
            labelGenerator: (id) => formatMac(id)
        };
    }

    /**
     * Create compact timeline configuration (for sidebar use)
     */
    function getCompactConfig() {
        return {
            title: 'BT Devices',
            mode: 'bluetooth',
            visualMode: 'compact',
            collapsed: false,
            showAnnotations: false,
            showLegend: false,
            defaultWindow: '15m',
            availableWindows: ['5m', '15m', '30m'],
            filters: {
                hideBaseline: { enabled: false },
                showOnlyNew: { enabled: true, label: 'New', default: false },
                showOnlyBurst: { enabled: false }
            },
            customFilters: [],
            maxItems: 30,
            maxDisplayedLanes: 8
        };
    }

    // Public API
    return {
        // Normalization
        normalizeDevice: normalizeDevice,
        normalizeTscmDevice: normalizeTscmDevice,
        normalizeDevices: normalizeDevices,

        // Utilities
        rssiToStrength: rssiToStrength,
        classifyDevice: classifyDevice,
        formatMac: formatMac,
        isTracker: isTracker,

        // Configuration presets
        getBluetoothConfig: getBluetoothConfig,
        getCompactConfig: getCompactConfig,

        // Constants
        RSSI_THRESHOLDS: RSSI_THRESHOLDS,
        DEVICE_PATTERNS: DEVICE_PATTERNS
    };
})();

// Export for module systems
if (typeof module !== 'undefined' && module.exports) {
    module.exports = BluetoothTimelineAdapter;
}

window.BluetoothTimelineAdapter = BluetoothTimelineAdapter;
