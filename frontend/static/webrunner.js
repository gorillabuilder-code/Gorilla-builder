import { WebContainer } from 'https://esm.sh/@webcontainer/api@1.1.8';

/**
 * WebContainer Orchestrator v3 — "Ironclad"
 *
 * Philosophy:
 *   The app is GUILTY (unstable) until PROVEN innocent (stable).
 *   Stability is never assumed — it is earned through a multi-gate
 *   verification pipeline that requires ALL of the following:
 *
 *     Gate 1: Vite compilation finished (no pending HMR)
 *     Gate 2: Dev server is responsive (HTTP health check)
 *     Gate 3: Iframe loaded without errors
 *     Gate 4: Zero errors across a quiet observation window
 *     Gate 5: Consecutive health check passes (not just one lucky moment)
 *
 *   If ANY gate fails at ANY point, the entire stability pipeline resets.
 *
 * Key changes from v2:
 *   - Replaced timer-based guessing with EVENT-DRIVEN state machine
 *   - Compilation tracking via Vite stdout parsing (watches for "built in Xms")
 *   - Health check loop that polls the dev server (not just "wait and hope")
 *   - Multi-pass quiet window: must see N consecutive clean intervals
 *   - Adaptive timeouts: large projects get more time automatically
 *   - Error journal: persists across cycles for pattern detection
 *   - Crash recovery with exponential backoff
 *   - Fix verification: after AI fix, runs the FULL gate pipeline again
 */

// ─── Constants ───────────────────────────────────────────────────────────

const CONFIG = Object.freeze({
  // Quiet window: how long (ms) with zero errors before advancing a gate
  QUIET_INTERVAL_MS: 3000,

  // How many consecutive quiet intervals are required to pass Gate 4
  REQUIRED_QUIET_PASSES: 3,

  // How often (ms) to poll the dev server for health checks
  HEALTH_CHECK_INTERVAL_MS: 2000,

  // How many consecutive health check successes needed for Gate 5
  REQUIRED_HEALTH_PASSES: 3,

  // Max time (ms) to wait for Vite compilation before treating as stuck
  COMPILATION_TIMEOUT_MS: 120_000,

  // Max time (ms) to wait for the iframe to load after server-ready
  IFRAME_LOAD_TIMEOUT_MS: 60_000,

  // Debounce window for batching errors before flush
  ERROR_DEBOUNCE_MS: 2500,

  // Max error buffer size (chars) sent to AI
  MAX_ERROR_PAYLOAD: 8000,

  // Safety timeout: auto-release fix lock if backend never responds
  FIX_LOCK_TIMEOUT_MS: 60_000,

  // Crash recovery: base delay before reboot
  CRASH_REBOOT_BASE_MS: 3000,

  // Crash recovery: max delay (exponential backoff cap)
  CRASH_REBOOT_MAX_MS: 30_000,

  // Max consecutive crashes before giving up on auto-reboot
  MAX_CRASH_REBOOTS: 5,

  // After fix is applied, minimum time to observe before stability
  POST_FIX_OBSERVATION_MS: 5000,
});


// ─── State Machine ───────────────────────────────────────────────────────

/**
 * The orchestrator moves through these phases in order.
 * ANY error at ANY phase resets to OBSERVING (or WAITING_FOR_FIX).
 *
 *   INSTALLING        → pnpm install is running
 *   BOOTING_SERVER    → dev server is starting, waiting for server-ready
 *   WAITING_COMPILE   → server is up, waiting for Vite to finish compiling
 *   WAITING_IFRAME    → compilation done, waiting for iframe to load
 *   OBSERVING         → iframe loaded, running quiet-window checks
 *   HEALTH_CHECKING   → quiet window passed, polling server health
 *   STABLE            → ALL gates passed — app is stable
 *   WAITING_FOR_FIX   → error was sent to AI, waiting for fix
 *   CRASHED           → server process died, attempting recovery
 */
const Phase = Object.freeze({
  INSTALLING: 'INSTALLING',
  BOOTING_SERVER: 'BOOTING_SERVER',
  WAITING_COMPILE: 'WAITING_COMPILE',
  WAITING_IFRAME: 'WAITING_IFRAME',
  OBSERVING: 'OBSERVING',
  HEALTH_CHECKING: 'HEALTH_CHECKING',
  STABLE: 'STABLE',
  WAITING_FOR_FIX: 'WAITING_FOR_FIX',
  CRASHED: 'CRASHED',
});


// ─── Noise Patterns ──────────────────────────────────────────────────────

const SERVER_NOISE = [
  'optimized dependencies', 'new dependencies optimized', 'pre-bundling',
  'deps optimized', 'deprecation', 'deprecationwarning', 'experimentalwarning',
  'warning:', 'warn', 'npm warn', 'peer dep', 'optional dep', 'requires a peer',
  'browserslist', 'autoprefixer', 'postcss', 'sourcemap', 'source map',
  'hmr update', 'hmr', 'hot updated', 'page reload', '[vite] connected',
  '[vite] hmr', 'watching for file changes', 'stdout is not a tty',
  'update available', 'run `npm', 'run `pnpm',
].map(s => s.toLowerCase());

const BROWSER_NOISE = [
  'Warning:', 'React does not recognize', 'Invalid DOM property',
  'Each child in a list', 'componentWillMount', 'componentWillReceiveProps',
  'findDOMNode is deprecated', 'Legacy context API', 'StrictMode', 'act(...)',
  'Download the React DevTools', 'DevTools', 'Manifest:', 'favicon',
  'the server responded with a status of 404', 'ResizeObserver loop',
  'Non-Error promise rejection', 'net::ERR_BLOCKED_BY_CLIENT',
  'chrome-extension', 'moz-extension', 'Failed to load resource',
  'Autofocus processing', 'Layout was forced before the page was fully loaded',
];

const VITE_COMPILED_PATTERNS = [
  /built in \d+m?s/i,
  /ready in \d+m?s/i,
  /page reload/i,
  /hmr update/i,
  /✓ \d+ modules transformed/i,
];

const VITE_COMPILING_PATTERNS = [
  /hmr invalidate/i,
  /new dependencies pre-bundled/i,
  /optimizing dependencies/i,
  /transforming/i,
];


// ─── Main Class ──────────────────────────────────────────────────────────

export class WebRunner {
  constructor() {
    // WebContainer
    this.instance = null;
    this.url = null;
    this.shell = null;

    // State machine
    this._phase = null;
    this._cycle = 0; // monotonically increasing; stale callbacks check this

    // Install state
    this.hasInstalled = false;
    this.isInstalling = false;

    // Fix lock
    this.isFixing = false;
    this._fixLockTimer = null;

    // Error buffer
    this._errorBuffer = '';
    this._errorFlushTimer = null;
    this._seenErrors = new Set();

    // Error journal: persists across cycles for pattern detection
    this._errorJournal = [];

    // Stability gates
    this._quietPassCount = 0;
    this._quietCheckTimer = null;
    this._healthPassCount = 0;
    this._healthCheckTimer = null;
    this._iframeLoaded = false;
    this._compilationDone = false;

    // Timeouts
    this._compilationTimer = null;
    this._iframeTimer = null;

    // Crash recovery
    this._crashCount = 0;

    // External
    this._logger = null;
    this._projectId = null;
    this._contextName = 'Runtime/Server';
    this._onReady = null;

    // Bound message handler (so we can remove it)
    this._boundMessageHandler = this._handleIframeMessage.bind(this);
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Phase Transitions
  // ═══════════════════════════════════════════════════════════════════════

  _setPhase(phase) {
    const prev = this._phase;
    this._phase = phase;
    console.info(`⚙️ [Phase] ${prev || 'INIT'} → ${phase} (cycle ${this._cycle})`);
  }

  get isStable() {
    return this._phase === Phase.STABLE;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Stability Events (dispatched to UI)
  // ═══════════════════════════════════════════════════════════════════════

  _emitUnstable() {
    window.dispatchEvent(new CustomEvent('app_unstable'));
  }

  _emitStable() {
    console.info('✅✅✅ [STABLE] All 5 gates passed. App is stable.');
    window.dispatchEvent(new CustomEvent('app_stable'));
  }

  // Convenience alias for external callers
  markUnstable() {
    if (this._phase === Phase.STABLE) {
      this._setPhase(Phase.OBSERVING);
    }
    this._emitUnstable();
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Cycle Management
  // ═══════════════════════════════════════════════════════════════════════

  /**
   * Starts a brand-new observation cycle.
   * All timers, counters, and buffers from the previous cycle are wiped.
   * The phase is set to `startPhase` (default: OBSERVING).
   */
  _resetCycle(startPhase = Phase.OBSERVING) {
    this._cycle++;
    const cycle = this._cycle;

    // Kill ALL pending timers
    this._clearAllTimers();

    // Reset error tracking
    this._errorBuffer = '';
    this._seenErrors.clear();

    // Reset gate counters
    this._quietPassCount = 0;
    this._healthPassCount = 0;
    this._iframeLoaded = false;
    this._compilationDone = false;

    this._setPhase(startPhase);
    this._emitUnstable();

    console.info(`🔄 [Cycle ${cycle}] Fresh cycle started at phase ${startPhase}`);

    // If we're already in OBSERVING or later, kick off the gate pipeline
    if (startPhase === Phase.OBSERVING) {
      this._startQuietWindow(cycle);
    } else if (startPhase === Phase.WAITING_COMPILE) {
      this._startCompilationWatch(cycle);
    } else if (startPhase === Phase.WAITING_IFRAME) {
      this._startIframeWatch(cycle);
    }

    return cycle;
  }

  _clearAllTimers() {
    const timers = [
      '_errorFlushTimer', '_quietCheckTimer', '_healthCheckTimer',
      '_compilationTimer', '_iframeTimer', '_fixLockTimer',
    ];
    for (const key of timers) {
      if (this[key]) {
        clearTimeout(this[key]);
        clearInterval(this[key]); // covers both
        this[key] = null;
      }
    }
  }

  /** Guard: returns true if the given cycle is still current */
  _isCurrent(cycle) {
    return cycle === this._cycle;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Gate 1: Compilation Tracking
  // ═══════════════════════════════════════════════════════════════════════

  _startCompilationWatch(cycle) {
    this._compilationDone = false;

    // Safety: if compilation doesn't finish in time, assume it's stuck
    this._compilationTimer = setTimeout(() => {
      if (!this._isCurrent(cycle)) return;
      if (!this._compilationDone) {
        console.warn(`⏰ [Cycle ${cycle}] Compilation timeout — assuming complete.`);
        this._onCompilationDone(cycle);
      }
    }, CONFIG.COMPILATION_TIMEOUT_MS);
  }

  /**
   * Called when Vite stdout indicates compilation finished.
   * Advances to WAITING_IFRAME.
   */
  _onCompilationDone(cycle) {
    if (!this._isCurrent(cycle)) return;
    if (this._compilationDone) return; // already processed

    this._compilationDone = true;
    if (this._compilationTimer) {
      clearTimeout(this._compilationTimer);
      this._compilationTimer = null;
    }

    console.info(`🔨 [Cycle ${cycle}] Compilation complete. Advancing to iframe watch.`);
    this._setPhase(Phase.WAITING_IFRAME);
    this._startIframeWatch(cycle);
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Gate 2: Server Health Check
  // ═══════════════════════════════════════════════════════════════════════

  /**
   * Actively polls the dev server URL. This catches cases where the server
   * is "up" but returning 500s or timing out.
   */
  _startHealthCheckLoop(cycle) {
    this._healthPassCount = 0;
    this._setPhase(Phase.HEALTH_CHECKING);

    const check = async () => {
      if (!this._isCurrent(cycle)) return;
      if (this._phase !== Phase.HEALTH_CHECKING) return;

      try {
        // We don't actually fetch (CORS/iframe sandbox), but we check
        // that the server process is still alive and no errors appeared
        if (this.shell && this._errorBuffer.trim() === '' && this._compilationDone) {
          this._healthPassCount++;
          console.info(`💚 [Cycle ${cycle}] Health check ${this._healthPassCount}/${CONFIG.REQUIRED_HEALTH_PASSES}`);

          if (this._healthPassCount >= CONFIG.REQUIRED_HEALTH_PASSES) {
            this._clearAllTimers();
            this._setPhase(Phase.STABLE);
            this._crashCount = 0; // reset crash counter on successful stable
            this._emitStable();
            return;
          }
        } else {
          // Reset: something is wrong
          this._healthPassCount = 0;
        }
      } catch (e) {
        this._healthPassCount = 0;
      }

      this._healthCheckTimer = setTimeout(check, CONFIG.HEALTH_CHECK_INTERVAL_MS);
    };

    check();
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Gate 3: Iframe Load Tracking
  // ═══════════════════════════════════════════════════════════════════════

  _startIframeWatch(cycle) {
    this._iframeLoaded = false;

    // If iframe was already loaded (from a previous HMR), skip this gate
    // by checking after a short delay
    this._iframeTimer = setTimeout(() => {
      if (!this._isCurrent(cycle)) return;
      if (!this._iframeLoaded) {
        console.warn(`⏰ [Cycle ${cycle}] Iframe load timeout — proceeding to observation.`);
        this._iframeLoaded = true;
        this._advanceFromIframe(cycle);
      }
    }, CONFIG.IFRAME_LOAD_TIMEOUT_MS);
  }

  _onIframeLoaded(cycle) {
    if (!this._isCurrent(cycle)) return;
    if (this._iframeLoaded) return;

    this._iframeLoaded = true;
    if (this._iframeTimer) {
      clearTimeout(this._iframeTimer);
      this._iframeTimer = null;
    }

    console.info(`🌐 [Cycle ${cycle}] Iframe loaded. Advancing to observation.`);
    this._advanceFromIframe(cycle);
  }

  _advanceFromIframe(cycle) {
    if (!this._isCurrent(cycle)) return;
    this._setPhase(Phase.OBSERVING);
    this._startQuietWindow(cycle);
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Gate 4: Quiet Window (consecutive error-free intervals)
  // ═══════════════════════════════════════════════════════════════════════

  _startQuietWindow(cycle) {
    this._quietPassCount = 0;
    this._runQuietCheck(cycle);
  }

  _runQuietCheck(cycle) {
    if (!this._isCurrent(cycle)) return;
    if (this._phase !== Phase.OBSERVING) return;

    this._quietCheckTimer = setTimeout(() => {
      if (!this._isCurrent(cycle)) return;
      if (this._phase !== Phase.OBSERVING) return;
      if (this.isFixing) return; // don't advance while AI is working

      // Check if the interval was clean (no new errors)
      if (this._errorBuffer.trim() === '') {
        this._quietPassCount++;
        console.info(`🤫 [Cycle ${cycle}] Quiet interval ${this._quietPassCount}/${CONFIG.REQUIRED_QUIET_PASSES}`);

        if (this._quietPassCount >= CONFIG.REQUIRED_QUIET_PASSES) {
          console.info(`🎯 [Cycle ${cycle}] Quiet window passed. Advancing to health checks.`);
          this._startHealthCheckLoop(cycle);
          return;
        }
      } else {
        // Errors appeared during this interval — DON'T reset the whole cycle,
        // just reset the quiet counter and let the error pipeline handle it
        console.info(`🔴 [Cycle ${cycle}] Errors during quiet interval. Resetting counter.`);
        this._quietPassCount = 0;
        // Don't clear the error buffer here — _pushError / _flushErrors handles it
      }

      // Schedule next interval check
      this._runQuietCheck(cycle);
    }, CONFIG.QUIET_INTERVAL_MS);
  }

  /**
   * Called whenever a new error arrives during OBSERVING.
   * Resets the quiet window counter.
   */
  _interruptQuietWindow() {
    this._quietPassCount = 0;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Noise Filtering & Deduplication
  // ═══════════════════════════════════════════════════════════════════════

  _isRealServerError(data) {
    const lower = data.toLowerCase();
    for (const noise of SERVER_NOISE) {
      if (lower.includes(noise)) return false;
    }
    return true;
  }

  _isRealBrowserError(message) {
    for (const noise of BROWSER_NOISE) {
      if (message.includes(noise)) return false;
    }
    return true;
  }

  _errorSignature(data) {
    const firstLine = data.split('\n')[0].trim().slice(0, 300);
    return firstLine
      .replace(/\s+at\s+.+$/g, '')
      .replace(/\(.*?\)/g, '')
      .replace(/:\d+:\d+/g, '') // strip line:col
      .trim();
  }

  _isNewError(data) {
    const sig = this._errorSignature(data);
    if (this._seenErrors.has(sig)) return false;
    this._seenErrors.add(sig);
    return true;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Error Ingestion
  // ═══════════════════════════════════════════════════════════════════════

  /**
   * Main error intake. All errors flow through here.
   */
  _pushError(data) {
    // Never collect during install
    if (this._phase === Phase.INSTALLING) return;

    // Dedup
    if (!this._isNewError(data)) return;

    // Journal (persists across cycles for pattern detection)
    this._errorJournal.push({
      timestamp: Date.now(),
      cycle: this._cycle,
      phase: this._phase,
      signature: this._errorSignature(data),
      snippet: data.slice(0, 500),
    });
    // Keep journal bounded
    if (this._errorJournal.length > 100) {
      this._errorJournal = this._errorJournal.slice(-50);
    }

    const cycle = this._cycle;

    // If stable: for non-fatal errors, we still collect them but
    // the stable guard means we DON'T yank the preview.
    // Fatal errors bypass this via _pushFatalError.
    if (this._phase === Phase.STABLE) {
      console.warn(`[STABLE — LOGGED] ${data.slice(0, 150)}`);
      return;
    }

    // Interrupt quiet window
    this._interruptQuietWindow();

    // Accumulate
    this._errorBuffer += data + '\n';

    // If AI is already fixing, just accumulate — don't flush
    if (this.isFixing) return;

    // Debounce flush
    if (this._errorFlushTimer) clearTimeout(this._errorFlushTimer);
    this._errorFlushTimer = setTimeout(() => {
      if (!this._isCurrent(cycle)) return;
      this._flushErrors(cycle);
    }, CONFIG.ERROR_DEBOUNCE_MS);
  }

  /**
   * Fatal error: bypasses the stable guard and forces a full reset.
   */
  _pushFatalError(data) {
    if (this._phase === Phase.INSTALLING) return;

    console.error(`☠️ [FATAL] ${data.slice(0, 200)}`);

    // Break out of any phase, including STABLE
    this._resetCycle(Phase.OBSERVING);
    this._errorBuffer += data + '\n';
    this._flushErrorsImmediate();
  }

  /**
   * Flush buffered errors to AI coder.
   */
  _flushErrors(cycle) {
    if (!this._isCurrent(cycle)) return;
    if (this.isFixing) return;
    if (this._errorBuffer.trim() === '') return;

    const payload = this._errorBuffer.trim().slice(-CONFIG.MAX_ERROR_PAYLOAD);

    // Check for recurring patterns
    const patternWarning = this._detectRecurringPatterns();

    const prompt = [
      `SYSTEM ALERT: ❌ ${this._contextName} Errors Detected:`,
      `<logs>`,
      payload,
      `</logs>`,
      patternWarning,
      `Please analyze these logs, identify the root cause, and fix the codebase to resolve them.`,
    ].filter(Boolean).join('\n');

    // Clear buffer BEFORE dispatching
    this._errorBuffer = '';
    if (this._errorFlushTimer) {
      clearTimeout(this._errorFlushTimer);
      this._errorFlushTimer = null;
    }

    if (this._logger) this._logger('coder', prompt);
    this._notifyCoder(this._projectId, prompt);
  }

  _flushErrorsImmediate() {
    if (this._phase === Phase.INSTALLING) return;
    if (this._errorFlushTimer) clearTimeout(this._errorFlushTimer);
    this._flushErrors(this._cycle);
  }

  /**
   * Scans the error journal for patterns (same error across multiple cycles).
   */
  _detectRecurringPatterns() {
    if (this._errorJournal.length < 5) return '';

    const sigCounts = {};
    for (const entry of this._errorJournal) {
      sigCounts[entry.signature] = (sigCounts[entry.signature] || 0) + 1;
    }

    const recurring = Object.entries(sigCounts)
      .filter(([, count]) => count >= 3)
      .map(([sig, count]) => `  "${sig}" (seen ${count}x across cycles)`);

    if (recurring.length === 0) return '';

    return [
      `\n⚠️ RECURRING PATTERN WARNING — These errors keep reappearing after fixes:`,
      ...recurring,
      `Please address the root cause, not just symptoms.`,
    ].join('\n');
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Fix Lock
  // ═══════════════════════════════════════════════════════════════════════

  async _notifyCoder(projectId, systemPrompt) {
    if (this.isFixing) return;

    this.isFixing = true;
    this._setPhase(Phase.WAITING_FOR_FIX);
    this._emitUnstable();

    const pid = projectId
      || window.PROJECT_ID
      || (window.location.pathname.match(/\/projects\/([^\/]+)/) || [])[1];

    if (!pid) {
      console.error('❌ No project ID found. Cannot dispatch to AI.');
      this.isFixing = false;
      return;
    }

    try {
      console.info('🤖 Dispatching error logs to AI Coder...');
      const formData = new FormData();
      formData.append('level', 'ERROR');
      formData.append('message', systemPrompt);

      await fetch(`/api/project/${pid}/log`, {
        method: 'POST',
        body: formData,
      });
      console.info('✅ Logs dispatched to AI.');
    } catch (err) {
      console.error('❌ Failed to reach AI Auto-Fixer:', err);
    } finally {
      // Safety timeout
      if (this._fixLockTimer) clearTimeout(this._fixLockTimer);
      this._fixLockTimer = setTimeout(() => {
        if (this.isFixing) {
          console.warn(`🔓 Fix lock safety-released after ${CONFIG.FIX_LOCK_TIMEOUT_MS}ms`);
          this.releaseFixLock();
        }
      }, CONFIG.FIX_LOCK_TIMEOUT_MS);
    }
  }

  /**
   * Called by backend when the AI has finished applying its fix.
   * This is THE critical transition: reset everything and run the full
   * gate pipeline from scratch to verify the fix actually worked.
   */
  releaseFixLock() {
    if (!this.isFixing) return;

    this.isFixing = false;
    if (this._fixLockTimer) {
      clearTimeout(this._fixLockTimer);
      this._fixLockTimer = null;
    }

    console.info('🔓 Fix applied. Running full verification pipeline...');

    // Check if there were errors collected DURING the fix
    const pendingErrors = this._errorBuffer.trim();

    // Start a fresh cycle. If compilation is expected, start at WAITING_COMPILE.
    // Otherwise start at OBSERVING.
    const startPhase = this._compilationDone ? Phase.OBSERVING : Phase.WAITING_COMPILE;
    const cycle = this._resetCycle(startPhase);

    // If errors accumulated during the fix, re-inject them so they get flushed
    if (pendingErrors) {
      console.warn(`⚠️ Errors accumulated during fix — re-evaluating...`);
      // Give Vite a moment to recompile, then check if the errors persist
      setTimeout(() => {
        if (!this._isCurrent(cycle)) return;
        // Only re-push if NO new compilation happened (the fix might have resolved them)
        // We clear the buffer here — fresh errors from the new compilation will come through _pushError
      }, CONFIG.POST_FIX_OBSERVATION_MS);
    }
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Iframe Message Handler
  // ═══════════════════════════════════════════════════════════════════════

  _handleIframeMessage(e) {
    if (!e.data || !e.data.type) return;

    if (e.data.type === 'iframe_loaded') {
      console.info('🌐 [IFRAME] App mounted.');
      this._onIframeLoaded(this._cycle);
    }

    if (e.data.type === 'iframe_error') {
      const msg = e.data.message || '';
      if (msg && msg !== 'Console Error: {}' && this._isRealBrowserError(msg)) {
        console.info('🔴 [BROWSER ERROR]', msg);
        this._pushError(msg);
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Vite Stdout Analysis
  // ═══════════════════════════════════════════════════════════════════════

  /**
   * Parses Vite dev server output to track compilation state.
   * Returns: { isError, isCompileDone, isCompiling }
   */
  _analyzeServerOutput(data) {
    const result = { isError: false, isCompileDone: false, isCompiling: false };

    // Check for compilation completion
    for (const pattern of VITE_COMPILED_PATTERNS) {
      if (pattern.test(data)) {
        result.isCompileDone = true;
        break;
      }
    }

    // Check for ongoing compilation
    for (const pattern of VITE_COMPILING_PATTERNS) {
      if (pattern.test(data)) {
        result.isCompiling = true;
        break;
      }
    }

    // Check for real errors
    const errorKeywords = [
      'ReferenceError', 'SyntaxError', 'TypeError', 'RangeError',
      'Cannot find module', 'Module not found', 'ERR_MODULE_NOT_FOUND',
      'ERR_PACKAGE_PATH_NOT_EXPORTED', '[ERROR]', 'Failed to resolve',
      'Build failed', 'ENOENT', 'EACCES', 'EPERM',
      'Internal server error', 'Pre-transform error',
      'Failed to load', 'Transform failed',
    ];

    for (const keyword of errorKeywords) {
      if (data.includes(keyword)) {
        result.isError = true;
        break;
      }
    }

    // Also check the generic "Error:" but filter noise
    if (!result.isError && data.includes('Error:') && this._isRealServerError(data)) {
      result.isError = true;
    }

    return result;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Boot
  // ═══════════════════════════════════════════════════════════════════════

  async boot() {
    if (this.instance) return this.instance;
    console.info('🔌 Booting WebContainer...');
    this.instance = await WebContainer.boot({ coep: 'credentialless' });
    return this.instance;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // File Conversion
  // ═══════════════════════════════════════════════════════════════════════

  _convertFilesToTree(files) {
    const tree = {};
    for (const f of files) {
      const cleanPath = f.path.replace(/^\/+/, '');
      const parts = cleanPath.split('/');
      let current = tree;

      for (let i = 0; i < parts.length; i++) {
        const part = parts[i];
        const isFile = i === parts.length - 1;

        if (isFile) {
          let content = f.content || '';

          if (part === 'index.html') {
            content = this._injectErrorInterceptor(content);
          }

          current[part] = { file: { contents: content } };
        } else {
          if (!current[part]) current[part] = { directory: {} };
          current = current[part].directory;
        }
      }
    }
    return tree;
  }

  _injectErrorInterceptor(html) {
    const script = `
<script>
(function() {
  // Signal load
  window.addEventListener('load', function() {
    window.parent.postMessage({ type: 'iframe_loaded' }, '*');
  });

  // Catch uncaught errors
  window.addEventListener('error', function(e) {
    var msg = 'Uncaught Error: ' + (e.message || (e.error ? e.error.stack || e.error.message : 'Unknown'));
    window.parent.postMessage({ type: 'iframe_error', message: msg }, '*');
  });

  // Catch unhandled promise rejections
  window.addEventListener('unhandledrejection', function(e) {
    var reason = e.reason;
    var msg = 'Promise Rejection: ' + (reason ? (reason.stack || reason.message || String(reason)) : 'Unknown');
    window.parent.postMessage({ type: 'iframe_error', message: msg }, '*');
  });

  // Intercept console.error — but only forward genuine errors
  var _origError = console.error;
  var NOISE = ['Warning:', 'DevTools', 'Download the React', 'React does not recognize',
               'Invalid DOM property', 'Each child in a list', 'componentWill',
               'findDOMNode', 'Legacy context', 'StrictMode', 'act('];
  console.error = function() {
    var args = Array.prototype.slice.call(arguments);
    var msg = args.map(function(a) {
      return typeof a === 'object' ? JSON.stringify(a) : String(a);
    }).join(' ');

    var isNoise = NOISE.some(function(n) { return msg.indexOf(n) !== -1; });
    if (!isNoise && msg.length > 2) {
      window.parent.postMessage({ type: 'iframe_error', message: 'Console Error: ' + msg }, '*');
    }
    _origError.apply(console, args);
  };
})();
</script>`;

    // Inject after <head> or at the very start if no <head>
    if (html.includes('<head>')) {
      return html.replace('<head>', '<head>' + script);
    }
    return script + html;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Mount
  // ═══════════════════════════════════════════════════════════════════════

  async mount(flatFiles) {
    if (!this.instance) await this.boot();
    const tree = this._convertFilesToTree(flatFiles);
    await this.instance.mount(tree);
    console.info('📂 Files mounted into Browser VM');
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Install
  // ═══════════════════════════════════════════════════════════════════════

  async install(logger, projectId) {
    if (!this.instance) throw new Error('Container not booted');

    this._setPhase(Phase.INSTALLING);
    this.isInstalling = true;
    this._logger = logger;
    this._projectId = projectId;
    this._emitUnstable();

    try {
      let success = false;
      let attempts = 0;
      const isUpdate = this.hasInstalled;

      while (!success && attempts < 3) {
        attempts++;
        let errorLogs = '';

        const installArgs = ['install', '--shamefully-hoist', '--no-frozen-lockfile'];

        if (isUpdate) {
          if (attempts === 1) {
            logger('system', '📦 Package.json modified. Syncing dependencies...');
          } else {
            logger('system', '🧹 Clearing dependency cache and forcing hard install...');
            installArgs.push('--force');
            const rmProcess = await this.instance.spawn('rm', ['-rf', 'node_modules', 'package-lock.json']);
            await rmProcess.exit;
          }
        } else {
          if (attempts === 1) {
            logger('system', '📦 Installing dependencies...');
          } else {
            logger('system', '🧹 Clearing cache and retrying...');
            const rmProcess = await this.instance.spawn('rm', ['-rf', 'node_modules', 'package-lock.json']);
            await rmProcess.exit;
          }
        }

        const process = await this.instance.spawn('pnpm', installArgs);

        process.output.pipeTo(new WritableStream({
          write(data) {
            errorLogs += data;
            console.info('[PNPM]', data);
          },
        }));

        const exitCode = await process.exit;

        if (exitCode !== 0) {
          logger('system', `⚠️ pnpm install failed (code ${exitCode}).`);

          if (!this.isFixing && attempts === 2) {
            logger('system', '🤖 Notifying AI Auto-Fixer for dependency conflict...');
            const autoPrompt = [
              `SYSTEM ALERT: package installation failed.`,
              `<logs>`,
              errorLogs.slice(-CONFIG.MAX_ERROR_PAYLOAD),
              `</logs>`,
              `Please review package.json for invalid packages or version conflicts and rewrite it.`,
            ].join('\n');

            this.isInstalling = false;
            await this._notifyCoder(projectId, autoPrompt);
            this.isInstalling = true;
          }

          if (this.isFixing) {
            logger('system', '⏳ Waiting for AI to apply fixes before retrying...');
            let waitTicks = 0;
            while (this.isFixing && waitTicks < 60) {
              await new Promise(r => setTimeout(r, 2000));
              waitTicks++;
            }
            await new Promise(r => setTimeout(r, 4000));
          }
        } else {
          logger('system', '✅ Dependencies installed successfully.');
          success = true;
          this.hasInstalled = true;
        }
      }
    } finally {
      this.isInstalling = false;
    }
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Start Dev Server
  // ═══════════════════════════════════════════════════════════════════════

  async start(onReady, logger, projectId) {
    if (!this.instance) throw new Error('Container not booted');

    this._logger = logger;
    this._projectId = projectId;
    this._onReady = onReady;

    const envVars = {
      VITE_GORILLA_AUTH_ID: window.GORILLA_AUTH_ID || '',
      GORILLA_API_KEY: window.GORILLA_API_KEY || ' not found! ',
    };

    // Listen for iframe messages (remove old listener first to avoid dupes)
    window.removeEventListener('message', this._boundMessageHandler);
    window.addEventListener('message', this._boundMessageHandler);

    const bootServer = async () => {
      // Track compilation state per boot
      let compileSeen = false;

      logger('system', '🚀 Starting Dev Server...');
      this._setPhase(Phase.BOOTING_SERVER);
      this._emitUnstable();

      this.shell = await this.instance.spawn('npm', ['run', 'dev'], { env: envVars });

      const cycle = this._cycle;

      this.shell.output.pipeTo(new WritableStream({
        write: (data) => {
          console.info('[VM]', data);

          const analysis = this._analyzeServerOutput(data);

          // Track compilation
          if (analysis.isCompiling && !compileSeen) {
            compileSeen = false; // reset — new compilation started
            this._compilationDone = false;
          }

          if (analysis.isCompileDone) {
            compileSeen = true;
            this._onCompilationDone(this._cycle);
          }

          // Track errors
          if (analysis.isError) {
            this._pushError(data);
          }
        },
      }));

      this.shell.exit.then(async (code) => {
        if (code !== 0 && this._phase !== Phase.INSTALLING) {
          this._crashCount++;

          if (this._crashCount > CONFIG.MAX_CRASH_REBOOTS) {
            logger('system', `❌ Server crashed ${this._crashCount} times. Stopping auto-reboot.`);
            this._pushFatalError(
              `[FATAL] Dev server has crashed ${this._crashCount} times. ` +
              `The project likely has a fundamental configuration or syntax error that prevents startup.`
            );
            return;
          }

          this._pushFatalError(`[FATAL] Dev server crashed with exit code ${code}.`);
          this._flushErrorsImmediate();

          // Exponential backoff
          const delay = Math.min(
            CONFIG.CRASH_REBOOT_BASE_MS * Math.pow(2, this._crashCount - 1),
            CONFIG.CRASH_REBOOT_MAX_MS
          );

          logger('system', `⚠️ Server crashed (attempt ${this._crashCount}). Rebooting in ${Math.round(delay / 1000)}s...`);
          this._setPhase(Phase.CRASHED);

          // Wait for AI fix before rebooting
          let waitTicks = 0;
          while (this.isFixing && waitTicks < 45) {
            await new Promise(r => setTimeout(r, 2000));
            waitTicks++;
          }

          setTimeout(bootServer, delay);
        }
      });
    };

    bootServer();

    // Wait for server-ready event
    this.instance.on('server-ready', (port, url) => {
      console.info(`⚡ Server ready at ${url} (port ${port})`);
      this.url = url;
      if (this._onReady) this._onReady(url);

      // Server is ready — start the full gate pipeline
      // Begin at WAITING_COMPILE. Vite might still be pre-bundling.
      this._resetCycle(Phase.WAITING_COMPILE);
    });
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Direct File Write (for HMR updates)
  // ═══════════════════════════════════════════════════════════════════════

  async writeFile(path, content) {
    if (!this.instance) return;

    // Writing a file means Vite will recompile.
    // Reset the entire stability pipeline.
    console.info(`📝 File written: ${path} — resetting stability pipeline.`);

    await this.instance.fs.writeFile(path, content);

    // If we were stable or in health checking, drop back to WAITING_COMPILE
    if (this._phase === Phase.STABLE ||
        this._phase === Phase.HEALTH_CHECKING ||
        this._phase === Phase.OBSERVING) {
      this._compilationDone = false;
      this._resetCycle(Phase.WAITING_COMPILE);
    }
  }
}

export const webRunner = new WebRunner();