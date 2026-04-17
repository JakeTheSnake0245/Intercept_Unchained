/**
 * Ground Station Live Waterfall — Phase 5
 *
 * Subscribes to /ws/satellite_waterfall, receives binary frames in the same
 * wire format as the main listening-post waterfall, and renders them onto the
 * <canvas id="gs-waterfall"> element in satellite_dashboard.html.
 *
 * Wire frame format (matches utils/waterfall_fft.build_binary_frame):
 *   [uint8  msg_type=0x01]
 *   [float32 start_freq_mhz]
 *   [float32 end_freq_mhz]
 *   [uint16  bin_count]
 *   [uint8[] bins]       — 0=noise floor, 255=strongest signal
 */

(function () {
    'use strict';

    const CANVAS_ID = 'gs-waterfall';
    const ROW_HEIGHT = 2;  // px per waterfall row
    const SCROLL_STEP = ROW_HEIGHT;

    let _ws = null;
    let _canvas = null;
    let _ctx = null;
    let _offscreen = null;  // offscreen ImageData buffer
    let _reconnectTimer = null;
    let _centerMhz = 0;
    let _spanMhz = 0;
    let _connected = false;

    // -----------------------------------------------------------------------
    // Colour palette — 256-entry RGB array (matches listening-post waterfall)
    // -----------------------------------------------------------------------
    const _palette = _buildPalette();

    function _buildPalette() {
        const p = new Uint8Array(256 * 3);
        for (let i = 0; i < 256; i++) {
            let r, g, b;
            if (i < 64) {
                // black → dark blue
                r = 0; g = 0; b = Math.round(i * 2);
            } else if (i < 128) {
                // dark blue → cyan
                const t = (i - 64) / 64;
                r = 0; g = Math.round(t * 200); b = Math.round(128 + t * 127);
            } else if (i < 192) {
                // cyan → yellow
                const t = (i - 128) / 64;
                r = Math.round(t * 255); g = 200; b = Math.round(255 - t * 255);
            } else {
                // yellow → white
                const t = (i - 192) / 64;
                r = 255; g = 200; b = Math.round(t * 255);
            }
            p[i * 3] = r; p[i * 3 + 1] = g; p[i * 3 + 2] = b;
        }
        return p;
    }

    // -----------------------------------------------------------------------
    // Public API
    // -----------------------------------------------------------------------

    window.GroundStationWaterfall = {
        init,
        connect,
        disconnect,
        isConnected: () => _connected,
        setCenterFreq: (mhz, span) => { _centerMhz = mhz; _spanMhz = span; },
    };

    function init() {
        _canvas = document.getElementById(CANVAS_ID);
        if (!_canvas) return;
        _ctx = _canvas.getContext('2d');
        _resizeCanvas();
        window.addEventListener('resize', _resizeCanvas);
        _drawPlaceholder();
    }

    function connect() {
        if (_ws && (_ws.readyState === WebSocket.CONNECTING || _ws.readyState === WebSocket.OPEN)) {
            return;
        }
        if (_reconnectTimer) {
            clearTimeout(_reconnectTimer);
            _reconnectTimer = null;
        }

        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${location.host}/ws/satellite_waterfall`;

        try {
            _ws = new WebSocket(url);
            _ws.binaryType = 'arraybuffer';

            _ws.onopen = () => {
                _connected = true;
                _updateStatus('LIVE');
                console.log('[GS Waterfall] WebSocket connected');
            };

            _ws.onmessage = (evt) => {
                if (evt.data instanceof ArrayBuffer) {
                    _handleFrame(evt.data);
                }
            };

            _ws.onclose = () => {
                _connected = false;
                _updateStatus('DISCONNECTED');
                _scheduleReconnect();
            };

            _ws.onerror = (e) => {
                console.warn('[GS Waterfall] WebSocket error', e);
            };
        } catch (e) {
            console.error('[GS Waterfall] Failed to create WebSocket', e);
            _scheduleReconnect();
        }
    }

    function disconnect() {
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
        if (_ws) { _ws.close(); _ws = null; }
        _connected = false;
        _updateStatus('STOPPED');
        _drawPlaceholder();
    }

    // -----------------------------------------------------------------------
    // Frame rendering
    // -----------------------------------------------------------------------

    function _handleFrame(buf) {
        const view = new DataView(buf);
        if (buf.byteLength < 11) return;

        const msgType = view.getUint8(0);
        if (msgType !== 0x01) return;

        // const startFreq = view.getFloat32(1, true);  // little-endian
        // const endFreq   = view.getFloat32(5, true);
        const binCount  = view.getUint16(9, true);
        if (buf.byteLength < 11 + binCount) return;

        const bins = new Uint8Array(buf, 11, binCount);

        if (!_canvas || !_ctx) return;

        const W = _canvas.width;
        const H = _canvas.height;

        // Scroll existing image up by ROW_HEIGHT pixels
        if (!_offscreen || _offscreen.width !== W || _offscreen.height !== H) {
            _offscreen = _ctx.getImageData(0, 0, W, H);
        } else {
            _offscreen = _ctx.getImageData(0, 0, W, H);
        }

        // Shift rows up by ROW_HEIGHT
        const data = _offscreen.data;
        const rowBytes = W * 4;
        data.copyWithin(0, SCROLL_STEP * rowBytes);

        // Write new row(s) at the bottom
        const bottom = H - ROW_HEIGHT;
        for (let row = 0; row < ROW_HEIGHT; row++) {
            const rowStart = (bottom + row) * rowBytes;
            for (let x = 0; x < W; x++) {
                const binIdx = Math.floor((x / W) * binCount);
                const val = bins[Math.min(binIdx, binCount - 1)];
                const pi = val * 3;
                const di = rowStart + x * 4;
                data[di]     = _palette[pi];
                data[di + 1] = _palette[pi + 1];
                data[di + 2] = _palette[pi + 2];
                data[di + 3] = 255;
            }
        }

        _ctx.putImageData(_offscreen, 0, 0);
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    function _resizeCanvas() {
        if (!_canvas) return;
        const container = _canvas.parentElement;
        if (container) {
            _canvas.width  = container.clientWidth  || 400;
            _canvas.height = container.clientHeight || 200;
        }
        _offscreen = null;
        _drawPlaceholder();
    }

    function _drawPlaceholder() {
        if (!_ctx || !_canvas) return;
        _ctx.fillStyle = '#000a14';
        _ctx.fillRect(0, 0, _canvas.width, _canvas.height);
        _ctx.fillStyle = 'rgba(0,212,255,0.3)';
        _ctx.font = '12px monospace';
        _ctx.textAlign = 'center';
        _ctx.fillText('AWAITING SATELLITE PASS', _canvas.width / 2, _canvas.height / 2);
        _ctx.textAlign = 'left';
    }

    function _updateStatus(text) {
        const el = document.getElementById('gsWaterfallStatus');
        if (el) el.textContent = text;
    }

    function _scheduleReconnect(delayMs = 5000) {
        if (_reconnectTimer) return;
        _reconnectTimer = setTimeout(() => {
            _reconnectTimer = null;
            connect();
        }, delayMs);
    }

    // Auto-init when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
