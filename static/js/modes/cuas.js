/**
 * Intercept Unchained — C-UAS (Counter-UAS) Detection Module
 * IIFE pattern matching other modes.
 */
const CUAS = (function () {
    'use strict';

    let _sse = null;
    let _scanMode = 'passive';
    let _activeTab = 'all';
    let _detections = [];
    const MAX_ROWS = 500;

    const THREAT_CLASSES = {
        'CLEAR': 'clear',
        'POSSIBLE': 'possible',
        'PROBABLE': 'probable',
        'CONFIRMED': 'confirmed',
    };

    const CONFIDENCE_CLASSES = {
        'LOW': 'low',
        'MEDIUM': 'medium',
        'HIGH': 'high',
        'CONFIRMED': 'confirmed',
    };

    function getApiBase() {
        if (typeof currentAgent !== 'undefined' && currentAgent !== 'local') {
            return `/controller/agents/${currentAgent}/cuas`;
        }
        return '/cuas';
    }

    // ── Threat level strip ──────────────────────────────────

    function updateThreatLevel(level) {
        const el = document.getElementById('cuasThreatLevel');
        const label = document.getElementById('cuasThreatLabel');
        if (!el || !label) return;
        el.className = 'cuas-threat-level ' + (THREAT_CLASSES[level] || 'clear');
        label.textContent = level || 'CLEAR';
    }

    function updateSummary(data) {
        const det = document.getElementById('cuasDetCount');
        const sub = document.getElementById('cuasActiveSubtools');
        if (det) det.textContent = data.detections_count ?? _detections.length;
        if (sub) sub.textContent = data.active_subtools ?? 0;
        updateThreatLevel(data.threat_level || 'CLEAR');
    }

    // ── Subtool status pills ─────────────────────────────────

    function updateSubtoolStatus(subtools) {
        if (!subtools) return;
        Object.entries(subtools).forEach(([name, status]) => {
            const el = document.querySelector(`[data-subtool="${name}"]`);
            if (!el) return;
            el.className = 'cuas-subtool-status ' +
                (status.error ? 'error' : status.running ? 'running' : 'stopped');
        });
    }

    // ── Detection table ──────────────────────────────────────

    function _formatTime(ts) {
        if (!ts) return '--';
        return new Date(ts * 1000).toISOString().slice(11, 19);
    }

    function _freqOrId(d) {
        if (d.ssid) return d.ssid;
        if (d.bt_addr) return d.bt_addr;
        if (d.freq_mhz) return d.freq_mhz.toFixed(3) + ' MHz';
        return '--';
    }

    function renderDetection(d) {
        const body = document.getElementById('cuasDetBody');
        if (!body) return;

        // Remove placeholder row
        const placeholder = body.querySelector('td[colspan]');
        if (placeholder) placeholder.closest('tr').remove();

        // Dedup by detection_id
        if (_detections.find(x => x.detection_id === d.detection_id)) return;
        _detections.unshift(d);
        if (_detections.length > MAX_ROWS) _detections = _detections.slice(0, MAX_ROWS);

        const confClass = CONFIDENCE_CLASSES[d.confidence] || 'low';
        const row = document.createElement('tr');
        row.dataset.subtool = d.subtool || '';
        row.dataset.detId = d.detection_id;
        row.innerHTML = `
            <td>${_formatTime(d.ts_utc)}</td>
            <td><span class="cuas-det-type">${d.detection_type || '--'}</span></td>
            <td>${d.subtool || '--'}</td>
            <td>${_freqOrId(d)}</td>
            <td>${d.rssi_dbm != null ? d.rssi_dbm.toFixed(1) + ' dBm' : '--'}</td>
            <td>${d.drone_make || 'Unknown'}${d.drone_model && d.drone_model !== 'Unknown' ? ' / ' + d.drone_model : ''}</td>
            <td><span class="cuas-confidence ${confClass}">${d.confidence}</span></td>
        `;

        body.insertBefore(row, body.firstChild);
        applyTabFilter();
    }

    function applyTabFilter() {
        const rows = document.querySelectorAll('#cuasDetBody tr[data-subtool]');
        rows.forEach(row => {
            const show = _activeTab === 'all' || row.dataset.subtool === _activeTab;
            row.style.display = show ? '' : 'none';
        });
    }

    function rebuildTable() {
        const body = document.getElementById('cuasDetBody');
        if (!body) return;
        body.innerHTML = '';
        if (_detections.length === 0) {
            body.innerHTML = '<tr><td colspan="7" class="cuas-empty">No detections yet.</td></tr>';
            return;
        }
        _detections.forEach(d => {
            const confClass = CONFIDENCE_CLASSES[d.confidence] || 'low';
            const row = document.createElement('tr');
            row.dataset.subtool = d.subtool || '';
            row.dataset.detId = d.detection_id;
            row.innerHTML = `
                <td>${_formatTime(d.ts_utc)}</td>
                <td><span class="cuas-det-type">${d.detection_type || '--'}</span></td>
                <td>${d.subtool || '--'}</td>
                <td>${_freqOrId(d)}</td>
                <td>${d.rssi_dbm != null ? d.rssi_dbm.toFixed(1) + ' dBm' : '--'}</td>
                <td>${d.drone_make || 'Unknown'}${d.drone_model && d.drone_model !== 'Unknown' ? ' / ' + d.drone_model : ''}</td>
                <td><span class="cuas-confidence ${confClass}">${d.confidence}</span></td>
            `;
            body.appendChild(row);
        });
        applyTabFilter();
    }

    // ── Tab switching ────────────────────────────────────────

    function switchTab(tab) {
        _activeTab = tab;
        document.querySelectorAll('.cuas-tab').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.panel === tab);
        });
        const label = document.getElementById('cuasTabLabel');
        if (label) label.textContent = tab === 'all' ? 'ALL DETECTIONS' : tab.toUpperCase().replace('_', ' ');
        applyTabFilter();
    }

    // ── Scan mode ────────────────────────────────────────────

    function setScanMode(mode) {
        _scanMode = mode;
        document.querySelectorAll('.cuas-scan-mode-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.mode === mode);
        });
    }

    // ── SSE connection ───────────────────────────────────────

    function connectStream() {
        if (_sse) { _sse.close(); _sse = null; }

        _sse = new EventSource(`${getApiBase()}/stream`);

        _sse.addEventListener('detection', e => {
            try { renderDetection(JSON.parse(e.data)); } catch (_) {}
        });

        _sse.addEventListener('summary', e => {
            try { updateSummary(JSON.parse(e.data)); } catch (_) {}
        });

        _sse.onerror = () => {
            if (_sse) { _sse.close(); _sse = null; }
            setTimeout(connectStream, 5000);
        };
    }

    // ── API calls ────────────────────────────────────────────

    function startAll() {
        const deviceIndex = parseInt(document.getElementById('cuasDeviceIndex')?.value || '0', 10);
        const gain = parseInt(document.getElementById('cuasGain')?.value || '40', 10);

        fetch(`${getApiBase()}/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scan_mode: _scanMode, device_index: deviceIndex, gain }),
        })
        .then(r => r.json())
        .then(data => {
            updateSubtoolStatus(data.subtools);
            refreshStatus();
        })
        .catch(console.error);
    }

    function stopAll() {
        fetch(`${getApiBase()}/stop`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            updateSubtoolStatus(data.subtools);
            refreshStatus();
        })
        .catch(console.error);
    }

    function clearDetections() {
        fetch(`${getApiBase()}/clear`, { method: 'POST' })
        .then(() => {
            _detections = [];
            rebuildTable();
            updateThreatLevel('CLEAR');
            document.getElementById('cuasDetCount').textContent = '0';
        })
        .catch(console.error);
    }

    function refreshStatus() {
        fetch(`${getApiBase()}/status`)
        .then(r => r.json())
        .then(data => {
            updateSummary(data);
            updateSubtoolStatus(data.subtools);
        })
        .catch(console.error);
    }

    // ── Export ───────────────────────────────────────────────

    function exportDetections(format) {
        if (format === 'json') {
            const blob = new Blob([JSON.stringify(_detections, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a'); a.href = url;
            a.download = `cuas_detections_${Date.now()}.json`;
            a.click(); URL.revokeObjectURL(url);
        } else if (format === 'csv') {
            const headers = ['time','detection_type','subtool','freq_mhz','rssi_dbm','ssid','bssid','bt_addr','drone_make','drone_model','drone_serial','confidence'];
            const rows = _detections.map(d => headers.map(h => {
                const v = d[h];
                return v != null ? `"${String(v).replace(/"/g, '""')}"` : '';
            }).join(','));
            const csv = [headers.join(','), ...rows].join('\n');
            const blob = new Blob([csv], { type: 'text/csv' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a'); a.href = url;
            a.download = `cuas_detections_${Date.now()}.csv`;
            a.click(); URL.revokeObjectURL(url);
        }
    }

    // ── Init ─────────────────────────────────────────────────

    function init() {
        refreshStatus();
        connectStream();

        // Load existing detections
        fetch(`${getApiBase()}/detections`)
        .then(r => r.json())
        .then(data => {
            _detections = [];
            data.forEach(renderDetection);
        })
        .catch(() => {});
    }

    function destroy() {
        if (_sse) { _sse.close(); _sse = null; }
    }

    return {
        init,
        destroy,
        startAll,
        stopAll,
        clearDetections,
        refreshStatus,
        switchTab,
        setScanMode,
        exportDetections,
    };
})();
