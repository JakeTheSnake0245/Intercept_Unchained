/**
 * SSEManager - Centralized Server-Sent Events connection manager
 * Handles connection lifecycle, reconnection with exponential backoff,
 * visibility-based pause/resume, and state change notifications.
 */
const SSEManager = (function() {
    'use strict';

    const STATES = {
        CONNECTING: 'connecting',
        OPEN: 'open',
        RECONNECTING: 'reconnecting',
        CLOSED: 'closed',
        ERROR: 'error',
    };

    const BACKOFF_INITIAL = 1000;
    const BACKOFF_MAX = 30000;
    const BACKOFF_MULTIPLIER = 2;

    /** @type {Map<string, ConnectionEntry>} */
    const connections = new Map();

    /**
     * @typedef {Object} ConnectionEntry
     * @property {string} key
     * @property {string} url
     * @property {EventSource|null} source
     * @property {string} state
     * @property {number} backoff
     * @property {number|null} retryTimer
     * @property {boolean} intentionallyClosed
     * @property {Function|null} onMessage
     * @property {Function|null} onStateChange
     */

    function connect(key, url, options) {
        const opts = options || {};

        // Disconnect existing connection for this key
        if (connections.has(key)) {
            disconnect(key);
        }

        const entry = {
            key: key,
            url: url,
            source: null,
            state: STATES.CLOSED,
            backoff: BACKOFF_INITIAL,
            retryTimer: null,
            intentionallyClosed: false,
            onMessage: typeof opts.onMessage === 'function' ? opts.onMessage : null,
            onStateChange: typeof opts.onStateChange === 'function' ? opts.onStateChange : null,
        };

        connections.set(key, entry);
        openConnection(entry);
        return entry;
    }

    function openConnection(entry) {
        if (entry.intentionallyClosed) return;

        setState(entry, entry.state === STATES.CLOSED ? STATES.CONNECTING : STATES.RECONNECTING);

        try {
            const source = new EventSource(entry.url);
            entry.source = source;

            source.onopen = function() {
                entry.backoff = BACKOFF_INITIAL;
                setState(entry, STATES.OPEN);
            };

            source.onmessage = function(event) {
                if (entry.onMessage) {
                    try {
                        entry.onMessage(event);
                    } catch (err) {
                        console.debug('[SSEManager] onMessage error for ' + entry.key + ':', err);
                    }
                }
            };

            source.onerror = function() {
                // EventSource fires error on close and connection loss
                if (entry.intentionallyClosed) return;

                closeSource(entry);
                setState(entry, STATES.ERROR);
                scheduleReconnect(entry);
            };
        } catch (err) {
            setState(entry, STATES.ERROR);
            scheduleReconnect(entry);
        }
    }

    function closeSource(entry) {
        if (entry.source) {
            entry.source.onopen = null;
            entry.source.onmessage = null;
            entry.source.onerror = null;
            try { entry.source.close(); } catch (e) { /* ignore */ }
            entry.source = null;
        }
    }

    function scheduleReconnect(entry) {
        if (entry.intentionallyClosed) return;
        if (entry.retryTimer) return;

        // Pause reconnection when tab is hidden
        if (document.hidden) {
            setState(entry, STATES.RECONNECTING);
            return;
        }

        const delay = entry.backoff;
        entry.backoff = Math.min(entry.backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX);

        setState(entry, STATES.RECONNECTING);

        entry.retryTimer = window.setTimeout(function() {
            entry.retryTimer = null;
            if (!entry.intentionallyClosed) {
                openConnection(entry);
            }
        }, delay);
    }

    function disconnect(key) {
        const entry = connections.get(key);
        if (!entry) return;

        entry.intentionallyClosed = true;

        if (entry.retryTimer) {
            clearTimeout(entry.retryTimer);
            entry.retryTimer = null;
        }

        closeSource(entry);
        setState(entry, STATES.CLOSED);
        connections.delete(key);
    }

    function disconnectAll() {
        for (const key of Array.from(connections.keys())) {
            disconnect(key);
        }
    }

    function getState(key) {
        const entry = connections.get(key);
        return entry ? entry.state : STATES.CLOSED;
    }

    function getActiveKeys() {
        const keys = [];
        connections.forEach(function(entry, key) {
            if (entry.state === STATES.OPEN) {
                keys.push(key);
            }
        });
        return keys;
    }

    function setState(entry, newState) {
        if (entry.state === newState) return;
        const oldState = entry.state;
        entry.state = newState;

        if (entry.onStateChange) {
            try {
                entry.onStateChange(newState, oldState, entry.key);
            } catch (err) {
                console.debug('[SSEManager] onStateChange error:', err);
            }
        }

        // Update global indicator
        updateGlobalIndicator();
    }

    // --- Global SSE Status Indicator ---

    function updateGlobalIndicator() {
        const dot = document.getElementById('sseStatusDot');
        if (!dot) return;

        let hasOpen = false;
        let hasReconnecting = false;
        let hasError = false;

        connections.forEach(function(entry) {
            if (entry.state === STATES.OPEN) hasOpen = true;
            else if (entry.state === STATES.RECONNECTING || entry.state === STATES.CONNECTING) hasReconnecting = true;
            else if (entry.state === STATES.ERROR) hasError = true;
        });

        // Remove all state classes
        dot.classList.remove('online', 'warning', 'error', 'inactive');

        if (connections.size === 0) {
            dot.classList.add('inactive');
            dot.setAttribute('data-tooltip', 'No active streams');
        } else if (hasError && !hasOpen) {
            dot.classList.add('error');
            dot.setAttribute('data-tooltip', 'Stream connection error');
        } else if (hasReconnecting) {
            dot.classList.add('warning');
            dot.setAttribute('data-tooltip', 'Reconnecting...');
        } else if (hasOpen) {
            dot.classList.add('online');
            dot.setAttribute('data-tooltip', 'Streams connected');
        } else {
            dot.classList.add('inactive');
            dot.setAttribute('data-tooltip', 'Streams idle');
        }
    }

    // --- Visibility API: pause/resume reconnection ---

    document.addEventListener('visibilitychange', function() {
        if (document.hidden) return;

        // Tab became visible — reconnect any entries that were waiting
        connections.forEach(function(entry) {
            if (!entry.intentionallyClosed && !entry.source && !entry.retryTimer) {
                openConnection(entry);
            }
        });
    });

    return {
        STATES: STATES,
        connect: connect,
        disconnect: disconnect,
        disconnectAll: disconnectAll,
        getState: getState,
        getActiveKeys: getActiveKeys,
    };
})();
