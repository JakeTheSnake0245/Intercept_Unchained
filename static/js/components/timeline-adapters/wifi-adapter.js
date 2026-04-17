/**
 * WiFi Timeline Adapter
 * Normalizes WiFi network data for the Activity Timeline component
 * Used by: WiFi mode, TSCM (WiFi detections)
 */

const WiFiTimelineAdapter = (function() {
    'use strict';

    /**
     * RSSI to strength category mapping for WiFi
     */
    const RSSI_THRESHOLDS = {
        EXCELLENT: -50,    // 5 - excellent signal
        GOOD: -60,         // 4 - good signal
        FAIR: -70,         // 3 - fair signal
        WEAK: -80,         // 2 - weak signal
        POOR: -90          // 1 - very weak
    };

    /**
     * WiFi channel to frequency band mapping
     */
    const CHANNEL_BANDS = {
        // 2.4 GHz (channels 1-14)
        '2.4GHz': { min: 1, max: 14 },
        // 5 GHz (channels 32-177)
        '5GHz': { min: 32, max: 177 },
        // 6 GHz (channels 1-233, WiFi 6E)
        '6GHz': { min: 1, max: 233, is6e: true }
    };

    /**
     * Security type classifications
     */
    const SECURITY_TYPES = {
        OPEN: 'open',
        WEP: 'wep',
        WPA: 'wpa',
        WPA2: 'wpa2',
        WPA3: 'wpa3',
        ENTERPRISE: 'enterprise'
    };

    /**
     * Convert RSSI to strength category
     */
    function rssiToStrength(rssi) {
        if (rssi === null || rssi === undefined) return 3;

        const r = parseFloat(rssi);
        if (isNaN(r)) return 3;

        if (r > RSSI_THRESHOLDS.EXCELLENT) return 5;
        if (r > RSSI_THRESHOLDS.GOOD) return 4;
        if (r > RSSI_THRESHOLDS.FAIR) return 3;
        if (r > RSSI_THRESHOLDS.WEAK) return 2;
        return 1;
    }

    /**
     * Determine frequency band from channel
     */
    function getBandFromChannel(channel, frequency) {
        if (frequency) {
            const f = parseFloat(frequency);
            if (f >= 5925) return '6GHz';
            if (f >= 5000) return '5GHz';
            if (f >= 2400) return '2.4GHz';
        }

        const ch = parseInt(channel);
        if (isNaN(ch)) return 'unknown';

        // This is simplified - in practice 6GHz also uses channels 1+
        // but typically reported with frequency
        if (ch <= 14) return '2.4GHz';
        if (ch >= 32 && ch <= 177) return '5GHz';

        return 'unknown';
    }

    /**
     * Classify security type
     */
    function classifySecurity(network) {
        const security = (network.security || network.encryption || '').toLowerCase();
        const auth = (network.auth || '').toLowerCase();

        if (!security || security === 'none' || security === 'open') {
            return SECURITY_TYPES.OPEN;
        }
        if (security.includes('wep')) return SECURITY_TYPES.WEP;
        if (security.includes('wpa3')) return SECURITY_TYPES.WPA3;
        if (security.includes('wpa2') || security.includes('rsn')) {
            if (auth.includes('eap') || auth.includes('802.1x') || auth.includes('enterprise')) {
                return SECURITY_TYPES.ENTERPRISE;
            }
            return SECURITY_TYPES.WPA2;
        }
        if (security.includes('wpa')) return SECURITY_TYPES.WPA;

        return 'unknown';
    }

    /**
     * Truncate SSID for display
     */
    function formatSsid(ssid, maxLength = 20) {
        if (!ssid) return '[Hidden]';
        if (ssid.length <= maxLength) return ssid;
        return ssid.substring(0, maxLength - 3) + '...';
    }

    /**
     * Identify potentially interesting network characteristics
     */
    function identifyCharacteristics(network) {
        const characteristics = [];
        const ssid = (network.ssid || '').toLowerCase();

        // Hidden network
        if (!network.ssid || network.is_hidden) {
            characteristics.push('hidden');
        }

        // Open network
        if (classifySecurity(network) === SECURITY_TYPES.OPEN) {
            characteristics.push('open');
        }

        // Weak security
        if (classifySecurity(network) === SECURITY_TYPES.WEP) {
            characteristics.push('weak-security');
        }

        // Potential hotspot
        if (/hotspot|mobile|tether|android|iphone/i.test(ssid)) {
            characteristics.push('hotspot');
        }

        // Guest network
        if (/guest|visitor|public/i.test(ssid)) {
            characteristics.push('guest');
        }

        // IoT device
        if (/ring|nest|ecobee|smartthings|wyze|arlo|hue|lifx/i.test(ssid)) {
            characteristics.push('iot');
        }

        return characteristics;
    }

    /**
     * Normalize a WiFi network detection for the timeline
     */
    function normalizeNetwork(network) {
        const ssid = network.ssid || network.essid || '';
        const bssid = network.bssid || network.mac || '';
        const band = getBandFromChannel(network.channel, network.frequency);
        const security = classifySecurity(network);
        const characteristics = identifyCharacteristics(network);

        const tags = [band, security, ...characteristics];

        return {
            id: bssid || ssid,
            label: formatSsid(ssid) || formatMac(bssid),
            strength: rssiToStrength(network.rssi || network.signal),
            duration: network.duration || 1000,
            type: 'wifi',
            tags: tags.filter(Boolean),
            metadata: {
                ssid: ssid,
                bssid: bssid,
                channel: network.channel,
                frequency: network.frequency,
                rssi: network.rssi || network.signal,
                security: security,
                band: band,
                characteristics: characteristics
            }
        };
    }

    /**
     * Normalize for TSCM context
     */
    function normalizeTscmNetwork(network) {
        const normalized = normalizeNetwork(network);

        // Add TSCM-specific tags
        if (network.is_new) normalized.tags.push('new');
        if (network.threat_level) normalized.tags.push(`threat-${network.threat_level}`);
        if (network.is_rogue) normalized.tags.push('rogue');
        if (network.is_deauth_target) normalized.tags.push('targeted');

        normalized.metadata.threat_level = network.threat_level;
        normalized.metadata.first_seen = network.first_seen;
        normalized.metadata.client_count = network.client_count;

        return normalized;
    }

    /**
     * Format MAC/BSSID for display
     */
    function formatMac(mac) {
        if (!mac) return 'Unknown';
        return mac.toUpperCase();
    }

    /**
     * Batch normalize multiple networks
     */
    function normalizeNetworks(networks, context = 'scan') {
        const normalizer = context === 'tscm' ? normalizeTscmNetwork : normalizeNetwork;
        return networks.map(normalizer);
    }

    /**
     * Create timeline configuration for WiFi mode
     */
    function getWiFiConfig() {
        return {
            title: 'Network Activity',
            mode: 'wifi',
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
                    key: 'showOnlyOpen',
                    label: 'Open Only',
                    default: false,
                    predicate: (item) => item.tags.includes('open')
                },
                {
                    key: 'hideHidden',
                    label: 'Hide Hidden',
                    default: false,
                    predicate: (item) => !item.tags.includes('hidden')
                },
                {
                    key: 'show5GHz',
                    label: '5GHz Only',
                    default: false,
                    predicate: (item) => item.tags.includes('5GHz')
                }
            ],
            maxItems: 100,
            maxDisplayedLanes: 15,
            labelGenerator: (id) => formatSsid(id)
        };
    }

    /**
     * Create compact configuration for sidebar
     */
    function getCompactConfig() {
        return {
            title: 'Networks',
            mode: 'wifi',
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
        normalizeNetwork: normalizeNetwork,
        normalizeTscmNetwork: normalizeTscmNetwork,
        normalizeNetworks: normalizeNetworks,

        // Utilities
        rssiToStrength: rssiToStrength,
        getBandFromChannel: getBandFromChannel,
        classifySecurity: classifySecurity,
        formatSsid: formatSsid,
        identifyCharacteristics: identifyCharacteristics,

        // Configuration presets
        getWiFiConfig: getWiFiConfig,
        getCompactConfig: getCompactConfig,

        // Constants
        RSSI_THRESHOLDS: RSSI_THRESHOLDS,
        SECURITY_TYPES: SECURITY_TYPES
    };
})();

// Export for module systems
if (typeof module !== 'undefined' && module.exports) {
    module.exports = WiFiTimelineAdapter;
}

window.WiFiTimelineAdapter = WiFiTimelineAdapter;
