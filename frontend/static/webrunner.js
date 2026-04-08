import { WebContainer } from 'https://esm.sh/@webcontainer/api@1.1.8';

/**
 * WebContainer Orchestrator v2
 * 
 * Core fix: introduces a "cycle" model for error detection.
 * 
 * After every fix lands (releaseFixLock), we:
 *   1. Reset ALL error buffers and timers
 *   2. Increment a cycle ID (so stale callbacks from old cycles are ignored)
 *   3. Start a fresh observation window before declaring stability
 * 
 * This ensures that fixing one error never masks a second one.
 */
export class WebRunner {
    constructor() {
        this.instance = null;
        this.url = null;
        this.shell = null;

        // --- Fix lock state ---
        this.isFixing = false;
        this.fixUnlockTimer = null;

        // --- Install state ---
        this.hasInstalled = false;
        this.isInstalling = false;

        // --- Stability state ---
        this.isStable = false;

        // --- Cycle tracking ---
        // Every time a fix completes or files are mounted, we bump this.
        // Any pending timer callbacks from a previous cycle are discarded.
        this._cycle = 0;

        // --- Timers (stored so we can cancel them) ---
        this._errorFlushTimer = null;
        this._allClearTimer = null;

        // --- Error buffer for the current cycle ---
        this._errorBuffer = "";

        // --- External references ---
        this._logger = null;
        this._projectId = null;
        this._contextName = "Runtime/Server";

        // --- Deduplication: track error signatures we've already seen this cycle ---
        this._seenErrors = new Set();

        // --- Noise filters ---
        // Server-side output lines that contain "Error" but are NOT real errors
        this._serverNoisePatterns = [
            'Optimized dependencies',
            'new dependencies optimized',
            'Pre-bundling',
            'deps optimized',
            'deprecation',
            'DeprecationWarning',
            'ExperimentalWarning',
            'Warning:',
            'WARN',
            'npm warn',
            'peer dep',
            'optional dep',
            'requires a peer',
            'Browserslist',
            'autoprefixer',
            'PostCSS',
            'sourcemap',
            'source map',
            'hmr update',
            'HMR',
            'hot updated',
            'page reload',
            '[vite] connected',
            '[vite] hmr',
            'watching for file changes',
        ];

        // Browser-side console.error messages that are warnings, not real errors
        this._browserNoisePatterns = [
            'Warning:',                          // React dev warnings
            'React does not recognize',          // React prop warnings
            'Invalid DOM property',              // React DOM warnings
            'Each child in a list',              // React key warnings
            'componentWillMount',                // React lifecycle deprecation
            'componentWillReceiveProps',
            'findDOMNode is deprecated',
            'Legacy context API',
            'StrictMode',
            'act(...)',                           // React testing noise
            'Download the React DevTools',
            'DevTools',
            'Manifest:',                         // PWA manifest
            'favicon',
            'the server responded with a status of 404',  // Missing assets (non-fatal)
            'ResizeObserver loop',               // Benign browser quirk
            'Non-Error promise rejection',
            'net::ERR_BLOCKED_BY_CLIENT',        // Ad blocker noise
            'chrome-extension',
            'moz-extension',
        ];
    }

    // ─── Noise Filtering & Deduplication ─────────────────────────────

    /**
     * Returns true if a server stdout line is actually a real error,
     * not just Vite/pnpm chatter that happens to contain "Error".
     */
    _isRealServerError(data) {
        const lower = data.toLowerCase();
        for (const noise of this._serverNoisePatterns) {
            if (lower.includes(noise.toLowerCase())) return false;
        }
        return true;
    }

    /**
     * Returns true if a browser-side console.error / iframe_error is a real
     * crash, not a React dev warning or browser quirk.
     */
    _isRealBrowserError(message) {
        for (const noise of this._browserNoisePatterns) {
            if (message.includes(noise)) return false;
        }
        return true;
    }

    /**
     * Extracts a short signature from an error message for dedup.
     * e.g. "ReferenceError: foo is not defined at App.jsx:12" → "ReferenceError: foo is not defined"
     */
    _errorSignature(data) {
        // Grab just the first meaningful line, strip file paths and line numbers
        const firstLine = data.split('\n')[0].trim().slice(0, 200);
        return firstLine.replace(/\s+at\s+.+$/, '').replace(/\(.*?\)/g, '').trim();
    }

    /**
     * Returns true if this error is new in the current cycle.
     * Prevents the same broken component from firing 50 identical errors.
     */
    _isNewError(data) {
        const sig = this._errorSignature(data);
        if (this._seenErrors.has(sig)) return false;
        this._seenErrors.add(sig);
        return true;
    }

    // ─── Boot ────────────────────────────────────────────────────────────

    async boot() {
        if (this.instance) return this.instance;
        console.log("Booting WebContainer...");
        this.instance = await WebContainer.boot({ coep: 'credentialless' });
        return this.instance;
    }

    // ─── Stability Events ────────────────────────────────────────────────

    markUnstable() {
        this.isStable = false;
        console.log("🔒 App marked unstable.");
        window.dispatchEvent(new CustomEvent('app_unstable'));
    }

    _markStable() {
        if (this.isStable) return; // already stable, don't spam
        this.isStable = true;
        console.info("✅ [ALL CLEAR] App is stable. Unlocking preview.");
        window.dispatchEvent(new CustomEvent('app_stable'));
    }

    // ─── Cycle Management ────────────────────────────────────────────────

    /**
     * Resets all error tracking state and starts a fresh observation window.
     * Called after every fix completes and after initial file mount.
     */
    _resetCycle(allClearDelayMs = 10000) {
        this._cycle++;
        const cycle = this._cycle;

        // Kill all pending timers from previous cycle
        if (this._errorFlushTimer) {
            clearTimeout(this._errorFlushTimer);
            this._errorFlushTimer = null;
        }
        if (this._allClearTimer) {
            clearTimeout(this._allClearTimer);
            this._allClearTimer = null;
        }

        // Wipe error buffer — old errors are irrelevant after a fix
        this._errorBuffer = "";

        // Clear dedup set so we can catch errors that reoccur after a fix
        this._seenErrors.clear();

        console.info(`🔄 [Cycle ${cycle}] Reset. Watching for errors (${allClearDelayMs}ms window)...`);

        // Start a fresh all-clear countdown
        this._startAllClear(allClearDelayMs, cycle);
    }

    /**
     * Starts (or restarts) the all-clear countdown for a given cycle.
     * If no errors arrive within `delayMs`, the app is declared stable.
     */
    _startAllClear(delayMs, cycle) {
        // Don't start timers during install
        if (this.isInstalling) return;

        // Cancel any existing all-clear timer
        if (this._allClearTimer) {
            clearTimeout(this._allClearTimer);
            this._allClearTimer = null;
        }

        this._allClearTimer = setTimeout(() => {
            // CRITICAL: Ignore if we've moved to a newer cycle
            if (cycle !== this._cycle) return;

            // If AI is still fixing, don't declare stable — just wait
            if (this.isFixing) {
                console.info(`⏳ [Cycle ${cycle}] All-clear deferred — AI is still fixing.`);
                return; // releaseFixLock will start a new cycle
            }

            this._markStable();
        }, delayMs);
    }

    // ─── Fix Lock ────────────────────────────────────────────────────────

    /**
     * Called by the backend when the AI has finished applying its fix.
     * This is THE critical transition point: reset everything and watch fresh.
     */
    releaseFixLock() {
        if (!this.isFixing) return;

        this.isFixing = false;
        if (this.fixUnlockTimer) {
            clearTimeout(this.fixUnlockTimer);
            this.fixUnlockTimer = null;
        }
        console.log("🔓 Backend signaled fix complete. Resetting error cycle.");

        // Start a fresh cycle with a short observation window.
        // 5s is enough for Vite HMR to recompile and for runtime errors to surface.
        this._resetCycle(5000);
    }

    // ─── Error Ingestion ─────────────────────────────────────────────────

    /**
     * Push an error string into the buffer. Errors are debounced and then
     * flushed to the AI coder as a batch.
     */
    _pushError(data) {
        // Ignore errors during install (they're expected)
        if (this.isInstalling) return;

        // STABLE GUARD: Once the app is stable and running, don't let
        // stray runtime errors (click handlers, lazy imports, etc.)
        // yank the preview away from the user. Just log them.
        if (this.isStable) {
            console.warn(`[STABLE — IGNORED] ${data.slice(0, 150)}`);
            return;
        }

        // Dedup: ignore identical errors we've already seen this cycle
        if (!this._isNewError(data)) {
            console.debug(`[DEDUP] Skipping duplicate error: ${this._errorSignature(data).slice(0, 80)}`);
            return;
        }

        const cycle = this._cycle;

        if (this.isFixing) {
            // IMPORTANT: While the AI is fixing, we still collect errors
            // but we DON'T flush them. They'll be relevant if the fix fails.
            // We ACCUMULATE (not replace) so nothing is lost.
            this._errorBuffer += data + "\n";
            return;
        }

        // Normal operation: accumulate and debounce
        this._errorBuffer += data + "\n";

        // Reset the flush timer (debounce: wait 2s for more errors to arrive)
        if (this._errorFlushTimer) clearTimeout(this._errorFlushTimer);
        this._errorFlushTimer = setTimeout(() => {
            if (cycle !== this._cycle) return; // stale cycle, ignore
            this._flushErrors(cycle);
        }, 2000);

        // Reset the all-clear timer — we just saw an error, so the app is NOT stable yet
        this._startAllClear(12000, cycle);
    }

    /**
     * Immediately flush accumulated errors to the AI coder.
     */
    _flushErrors(cycle) {
        if (cycle !== this._cycle) return;
        if (this.isFixing) return;
        if (this._errorBuffer.trim() === "") return;

        const truncated = this._errorBuffer.trim().slice(-6000);
        const prompt = `SYSTEM ALERT: ❌ ${this._contextName} Errors Detected:\n<logs>\n${truncated}\n</logs>\nPlease analyze these logs, identify the root cause, and fix the codebase to resolve them.`;

        // Clear the buffer BEFORE dispatching (so new errors during fix go into a fresh buffer)
        this._errorBuffer = "";

        if (this._errorFlushTimer) {
            clearTimeout(this._errorFlushTimer);
            this._errorFlushTimer = null;
        }

        if (this._logger) {
            this._logger("coder", prompt);
        }
        this._notifyCoder(this._projectId, prompt);
    }

    /**
     * Force-flush (used when the dev server crashes).
     */
    _flushErrorsImmediate() {
        if (this.isInstalling) return;
        if (this._errorFlushTimer) clearTimeout(this._errorFlushTimer);
        this._flushErrors(this._cycle);
    }

    /**
     * Push a FATAL error that bypasses the stable guard.
     * Used only for catastrophic events (server crash, process exit).
     * This will yank the preview and trigger a fix even if the app was stable.
     */
    _pushFatalError(data) {
        if (this.isInstalling) return;

        console.error(`[FATAL ERROR — bypassing stable guard]`, data.slice(0, 200));

        // Break out of stable state — something truly broke
        this.isStable = false;
        this.markUnstable();

        // Reset cycle so this gets a clean flush
        this._resetCycle(12000);

        // Push directly into the fresh buffer
        this._errorBuffer += data + "\n";
    }

    // ─── AI Coder Dispatch ───────────────────────────────────────────────

    async _notifyCoder(projectId, systemPrompt) {
        if (this.isFixing) return;

        this.isFixing = true;
        this.markUnstable();

        const pid = projectId ||
            window.PROJECT_ID ||
            (window.location.pathname.match(/\/projects\/([^\/]+)/) || [])[1];

        if (!pid) {
            this.isFixing = false;
            return;
        }

        try {
            console.info("🤖 Dispatching error logs to AI Coder...");
            const formData = new FormData();
            formData.append("level", "ERROR");
            formData.append("message", systemPrompt);

            await fetch(`/api/project/${pid}/log`, {
                method: 'POST',
                body: formData
            });
            console.info("✅ Logs dispatched to AI.");
        } catch (err) {
            console.info("❌ Failed to reach AI Auto-Fixer:", err);
        } finally {
            // Safety timeout: if the backend never calls releaseFixLock, auto-release after 45s
            if (this.fixUnlockTimer) clearTimeout(this.fixUnlockTimer);
            this.fixUnlockTimer = setTimeout(() => {
                if (this.isFixing) {
                    console.warn("🔓 AI Fix Lock safety-released via 45s timeout.");
                    this.releaseFixLock();
                }
            }, 45000);
        }
    }

    // ─── File Conversion ─────────────────────────────────────────────────

    _convertFilesToTree(files) {
        const tree = {};
        files.forEach(f => {
            const cleanPath = f.path.replace(/^\/+/, '');
            const parts = cleanPath.split('/');
            let current = tree;

            parts.forEach((part, index) => {
                const isFile = index === parts.length - 1;
                if (isFile) {
                    let content = f.content || "";

                    if (part === "index.html") {
                        const interceptor = `\n<script>
                            window.addEventListener('load', () => {
                                window.parent.postMessage({ type: 'iframe_loaded' }, '*');
                            });
                            window.addEventListener('error', (e) => {
                                window.parent.postMessage({ type: 'iframe_error', message: 'Uncaught Error: ' + (e.message || (e.error ? e.error.message : 'Unknown')) }, '*');
                            });
                            window.addEventListener('unhandledrejection', (e) => {
                                window.parent.postMessage({ type: 'iframe_error', message: 'Promise Rejection: ' + (e.reason ? (e.reason.message || e.reason) : 'Unknown') }, '*');
                            });
                            const _origError = console.error;
                            console.error = function(...args) {
                                const msg = args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ');
                                // Only forward genuine errors, not React/library dev warnings
                                if (msg.startsWith('Warning:') || msg.includes('DevTools') || msg.includes('Download the React')) {
                                    _origError.apply(console, args);
                                    return;
                                }
                                window.parent.postMessage({ type: 'iframe_error', message: 'Console Error: ' + msg }, '*');
                                _origError.apply(console, args);
                            };
                        </script>\n`;
                        content = content.replace('<head>', '<head>' + interceptor);
                    }

                    current[part] = { file: { contents: content } };
                } else {
                    if (!current[part]) current[part] = { directory: {} };
                    current = current[part].directory;
                }
            });
        });
        return tree;
    }

    // ─── Mount ───────────────────────────────────────────────────────────

    async mount(flatFiles) {
        if (!this.instance) await this.boot();
        const tree = this._convertFilesToTree(flatFiles);
        await this.instance.mount(tree);
        console.log("📂 Files mounted into Browser VM");
    }

    // ─── Install ─────────────────────────────────────────────────────────

    async install(logger, projectId) {
        if (!this.instance) throw new Error("Container not booted");

        this.isInstalling = true;
        this._logger = logger;
        this._projectId = projectId;
        this.markUnstable();

        try {
            let success = false;
            let attempts = 0;
            const isUpdate = this.hasInstalled;

            while (!success && attempts < 3) {
                attempts++;
                let errorLogs = "";

                let installArgs = ['install', '--shamefully-hoist', '--no-frozen-lockfile'];

                if (isUpdate) {
                    if (attempts === 1) {
                        logger("system", "📦 Package.json modified. Syncing dependencies...");
                    } else {
                        logger("system", "Clearing dependency cache and forcing hard install...");
                        installArgs.push('--force');
                        const rmProcess = await this.instance.spawn('rm', ['-rf', 'node_modules', 'package-lock.json']);
                        await rmProcess.exit;
                    }
                } else {
                    if (attempts === 1) {
                        logger("system", "Installing Dependencies (Fast Sync)...");
                    } else {
                        logger("system", "Clearing dependency cache and retrying...");
                        const rmProcess = await this.instance.spawn('rm', ['-rf', 'node_modules', 'package-lock.json']);
                        await rmProcess.exit;
                    }
                }

                const process = await this.instance.spawn('pnpm', installArgs);

                process.output.pipeTo(new WritableStream({
                    write(data) {
                        errorLogs += data;
                        console.info("[PNPM]", data);
                    }
                }));

                const exitCode = await process.exit;

                if (exitCode !== 0) {
                    logger("system", `⚠️ pnpm install failed (code ${exitCode}).`);

                    if (!this.isFixing && attempts === 2) {
                        logger("system", "Notifying AI Auto-Fixer for dependency conflict...");
                        const autoPrompt = `SYSTEM ALERT: package installation failed. Logs:\n<logs>\n${errorLogs.slice(-8000)}\n</logs>\nPlease review package.json for invalid packages or version conflicts and rewrite it.`;

                        this.isInstalling = false;
                        await this._notifyCoder(projectId, autoPrompt);
                        this.isInstalling = true;
                    }

                    if (this.isFixing) {
                        logger("system", "Waiting for AI to apply fixes before retrying...");
                        let waitTicks = 0;
                        while (this.isFixing && waitTicks < 60) {
                            await new Promise(r => setTimeout(r, 1000));
                            waitTicks++;
                        }
                        await new Promise(r => setTimeout(r, 2000));
                    }
                } else {
                    logger("system", "✅ Dependencies installed beautifully.");
                    success = true;
                    this.hasInstalled = true;
                }
            }
        } finally {
            this.isInstalling = false;
        }
    }

    // ─── Start Dev Server ────────────────────────────────────────────────

    async start(onReady, logger, projectId) {
        if (!this.instance) throw new Error("Container not booted");

        this._logger = logger;
        this._projectId = projectId;

        const envVars = {
            VITE_GORILLA_AUTH_ID: window.GORILLA_AUTH_ID || "",
            GORILLA_API_KEY: window.GORILLA_API_KEY || " not found! ",
        };

        // Listen for iframe messages
        window.addEventListener("message", (e) => {
            if (e.data && e.data.type === 'iframe_loaded') {
                console.info("🌐 [IFRAME] App mounted. Fast-tracking stability check.");
                // App is loaded — if no errors appear within 2s, declare stable
                this._startAllClear(2000, this._cycle);
            }

            if (e.data && e.data.type === 'iframe_error') {
                const msg = e.data.message || "";
                // Filter out empty errors, React dev warnings, browser quirks, etc.
                if (msg && msg !== "Console Error: {}" && this._isRealBrowserError(msg)) {
                    console.info("🔴 [BROWSER ERROR]", msg);
                    this._pushError(msg);
                }
            }
        });

        const bootServer = async () => {
            logger("system", "🚀 Starting Dev Server...");
            this.shell = await this.instance.spawn('npm', ['run', 'dev'], { env: envVars });

            this.shell.output.pipeTo(new WritableStream({
                write: (data) => {
                    // Only match genuine errors, not Vite chatter
                    const hasErrorKeyword = data.includes('ReferenceError') ||
                        data.includes('SyntaxError') ||
                        data.includes('TypeError') ||
                        data.includes('Cannot find module') ||
                        data.includes('Module not found') ||
                        data.includes('ERR_MODULE_NOT_FOUND') ||
                        data.includes('ERR_PACKAGE_PATH_NOT_EXPORTED') ||
                        data.includes('[ERROR]') ||
                        data.includes('Failed to resolve') ||
                        data.includes('Build failed') ||
                        data.includes('ENOENT') ||
                        (data.includes('Error:') && this._isRealServerError(data));

                    if (hasErrorKeyword) {
                        this._pushError(data);
                    }
                    console.info("[VM]", data);
                }
            }));

            this.shell.exit.then(async (code) => {
                if (code !== 0 && !this.isInstalling) {
                    this._pushFatalError(`[FATAL] Dev server crashed with code ${code}. Please fix the syntax or configuration errors.`);
                    this._flushErrorsImmediate();
                    logger("system", "⚠️ Server crashed. Rebooting in 3s...");

                    // Wait for AI fix before rebooting
                    let waitTicks = 0;
                    while (this.isFixing && waitTicks < 30) {
                        await new Promise(r => setTimeout(r, 1000));
                        waitTicks++;
                    }
                    setTimeout(bootServer, 3000);
                }
            });
        };

        bootServer();

        this.instance.on('server-ready', (port, url) => {
            console.log(`⚡ Server ready at ${url}`);
            this.url = url;
            onReady(url);

            // Initial cold boot: give Vite up to 25s, but iframe_loaded will shortcut this
            this._resetCycle(25000);
        });
    }

    // ─── Direct File Write ───────────────────────────────────────────────

    async writeFile(path, content) {
        if (!this.instance) return;
        await this.instance.fs.writeFile(path, content);
    }
}

export const webRunner = new WebRunner();