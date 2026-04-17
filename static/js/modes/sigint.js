/**
 * Intercept Unchained — SIGINT Distributed Signal Intelligence
 * IIFE pattern. Inspired by github.com/arall/sigint.
 */
const SIGINT = (function () {
    'use strict';

    let _sse = null;
    let _detections = [];
    let _filterProto = 'ALL';
    const MAX_ROWS = 500;

    const PROTO_CLASSES = {
        KEYFOB: 'keyfob', TPMS: 'tpms', PAGER: 'pager',
        PMR: 'pmr', ISM: 'ism', UNKNOWN: 'unknown',
    };

    function getApiBase() {
        if (typeof currentAgent !== 'undefined' && currentAgent !== 'local') {
            return `/controller/agents/${currentAgent}/sigint`;
        }
        return '/sigint';
    }

    function _ts(ts) {
        if (!ts) return '--';
        return new Date(ts * 1000).toISOString().slice(11, 19);
    }

    // ── Render ──────────────────────────────────────────────

    function renderDetection(d) {
        const body = document.getElementById('sigintDetBody');
        if (!body) return;

        const placeholder = body.querySelector('td[colspan]');
        if (placeholder) placeholder.closest('tr').remove();

        if (_detections.find(x => x.signal_id === d.signal_id)) return;
        _detections.unshift(d);
        if (_detections.length > MAX_ROWS) _detections = _detections.slice(0, MAX_ROWS);

        const proto = (d.protocol || 'UNKNOWN').toUpperCase();
        const protoClass = PROTO_CLASSES[proto] || 'unknown';

        const row = document.createElement('tr');
        row.dataset.proto = proto;
        row.innerHTML = `
            <td>${_ts(d.ts_utc)}</td>
            <td><span class="sigint-proto ${protoClass}">${proto}</span></td>
            <td>${d.freq_mhz ? d.freq_mhz.toFixed(4) : '--'}</td>
            <td>${d.rssi_dbm != null ? d.rssi_dbm.toFixed(1) + ' dBm' : '--'}</td>
            <td style="color:var(--text-dim)">${d.modulation || '--'}</td>
            <td style="word-break:break-word;max-width:200px">${d.message || '--'}</td>
        `;
        body.insertBefore(row, body.firstChild);
        applyProtoFilter();

        // Update count
        const cnt = document.getElementById('sigintDetCount');
        if (cnt) cnt.textContent = `${_detections.length} signals`;
    }

    function applyProtoFilter() {
        document.querySelectorAll('#sigintDetBody tr[data-proto]').forEach(row => {
            row.style.display = (_filterProto === 'ALL' || row.dataset.proto === _filterProto) ? '' : 'none';
        });
    }

    function filterProtocol(proto) {
        _filterProto = proto;
        document.querySelectorAll('[data-proto].cuas-tab').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.proto === proto);
        });
        applyProtoFilter();
    }

    // ── SSE ─────────────────────────────────────────────────

    function connectStream() {
        if (_sse) { _sse.close(); _sse = null; }
        _sse = new EventSource(`${getApiBase()}/stream`);

        _sse.addEventListener('detection', e => {
            try { renderDetection(JSON.parse(e.data)); } catch (_) {}
        });

        _sse.addEventListener('status', e => {
            try {
                const d = JSON.parse(e.data);
                const el = document.getElementById('sigintStatusText');
                if (el) el.textContent = d.msg || '';
            } catch (_) {}
        });

        _sse.onerror = () => {
            if (_sse) { _sse.close(); _sse = null; }
            setTimeout(connectStream, 5000);
        };
    }

    // ── API ──────────────────────────────────────────────────

    function start() {
        const band = document.getElementById('sigintBand')?.value || 'ism_433';
        const deviceIndex = parseInt(document.getElementById('sigintDeviceIndex')?.value || '0', 10);
        const gain = parseInt(document.getElementById('sigintGain')?.value || '40', 10);

        fetch(`${getApiBase()}/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scan_band: band, device_index: deviceIndex, gain }),
        })
        .then(r => r.json())
        .then(() => {
            const dot = document.getElementById('sigintStatusDot');
            if (dot) dot.className = 'status-dot running';
            const st = document.getElementById('sigintStatusText');
            if (st) st.textContent = 'Scanning…';
        })
        .catch(console.error);
    }

    function stop() {
        fetch(`${getApiBase()}/stop`, { method: 'POST' })
        .then(() => {
            const dot = document.getElementById('sigintStatusDot');
            if (dot) dot.className = 'status-dot inactive';
            const st = document.getElementById('sigintStatusText');
            if (st) st.textContent = 'Stopped';
        })
        .catch(console.error);
    }

    function clear() {
        fetch(`${getApiBase()}/clear`, { method: 'POST' })
        .then(() => {
            _detections = [];
            const body = document.getElementById('sigintDetBody');
            if (body) body.innerHTML = '<tr><td colspan="6" class="cuas-empty">No signals detected.</td></tr>';
            const cnt = document.getElementById('sigintDetCount');
            if (cnt) cnt.textContent = '0 signals';
        })
        .catch(console.error);
    }

    function exportCOT() {
        window.open(`${getApiBase()}/export/cot`, '_blank');
    }

    function exportJSON() {
        const blob = new Blob([JSON.stringify(_detections, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url;
        a.download = `sigint_detections_${Date.now()}.json`;
        a.click(); URL.revokeObjectURL(url);
    }

    // ── Init ─────────────────────────────────────────────────

    function init() {
        fetch(`${getApiBase()}/detections`)
        .then(r => r.json())
        .then(data => { _detections = []; data.forEach(renderDetection); })
        .catch(() => {});

        fetch(`${getApiBase()}/status`)
        .then(r => r.json())
        .then(data => {
            if (data.running) {
                const dot = document.getElementById('sigintStatusDot');
                if (dot) dot.className = 'status-dot running';
            }
        })
        .catch(() => {});

        connectStream();
    }

    function destroy() {
        if (_sse) { _sse.close(); _sse = null; }
    }

    return { init, destroy, start, stop, clear, filterProtocol, exportCOT, exportJSON };
})();
