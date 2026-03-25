import { WebContainer } from 'https://esm.sh/@webcontainer/api@1.1.8';

/**
 * WebContainer Orchestrator
 * Handles the browser-based Node.js runtime and Auto-Healing loops.
 */
export class WebRunner {
    constructor() {
        this.instance = null;
        this.url = null;
        this.shell = null;
        
        // 🛑 THE BOUNCER: Prevents the AI Clone War race condition
        this.isFixing = false; 
        this.fixUnlockTimer = null;
    }

    async boot() {
        if (this.instance) return this.instance;
        console.log("Booting WebContainer...");
        this.instance = await WebContainer.boot({ coep: 'credentialless' });
        return this.instance;
    }

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
                    
                    // 🛑 THE IFRAME INTERCEPTOR: Inject script to catch React/Vite Browser Errors!
                    if (part === "index.html") {
                        const interceptor = `\n<script>
                            window.addEventListener('error', (e) => {
                                window.parent.postMessage({ type: 'iframe_error', message: 'Uncaught Error: ' + (e.message || (e.error ? e.error.message : 'Unknown')) }, '*');
                            });
                            window.addEventListener('unhandledrejection', (e) => {
                                window.parent.postMessage({ type: 'iframe_error', message: 'Promise Rejection: ' + (e.reason ? (e.reason.message || e.reason) : 'Unknown') }, '*');
                            });
                            const _origError = console.error;
                            console.error = function(...args) {
                                window.parent.postMessage({ type: 'iframe_error', message: 'Console Error: ' + args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ') }, '*');
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

    async mount(flatFiles) {
        if (!this.instance) await this.boot();
        const tree = this._convertFilesToTree(flatFiles);
        await this.instance.mount(tree);
        console.log("📂 Files mounted into Browser VM");
    }

    _createDebouncedLogger(logger, contextName, projectId) {
        let buffer = "";
        let timeout = null;

        const flush = () => {
            if (buffer.trim() === "") return;

            // 🛑 THE PATIENT LOGGER: If AI is fixing, don't drop the error. Wait and try again!
            if (this.isFixing) {
                timeout = setTimeout(flush, 2000);
                return;
            }

            const truncatedBuffer = buffer.trim().slice(-8000);
            const prompt = `SYSTEM ALERT: ❌ ${contextName} Errors Detected:\n<logs>\n${truncatedBuffer}\n</logs>\nPlease analyze these logs, identify the root cause, and fix the codebase to resolve them.`;
            logger("coder", prompt); 
            this._notifyCoder(projectId, prompt); 
            buffer = "";
        };

        return {
            push: (data) => {
                buffer += data + "\n";
                if (timeout) clearTimeout(timeout);
                timeout = setTimeout(flush, 3000); 
            },
            flushImmediate: () => {
                if (timeout) clearTimeout(timeout);
                flush();
            }
        };
    }

    async _notifyCoder(projectId, systemPrompt) {
        if (this.isFixing) return;

        this.isFixing = true;
        const pid = projectId || window.PROJECT_ID || (window.location.pathname.match(/\/projects\/([^\/]+)/) || [])[1];
        
        if (!pid) {
            this.isFixing = false; 
            return;
        }

        try {
            console.info("🤖 Dispatching log to AI Coder...");
            const formData = new FormData();
            formData.append("level", "ERROR");
            formData.append("message", systemPrompt);

            await fetch(`/api/project/${pid}/log`, {
                method: 'POST',
                body: formData
            });
            console.info("✅ Logs successfully dispatched.");
        } catch (err) {
            console.info("❌ Failed to reach AI Auto-Fixer:", err);
        } finally {
            if (this.fixUnlockTimer) clearTimeout(this.fixUnlockTimer);
            // Ensure AI has enough time to write files before unlocking
            this.fixUnlockTimer = setTimeout(() => { 
                this.isFixing = false; 
                console.info("🔓 AI Fix Lock released.");
            }, 45000); 
        }
    }

    async install(logger, projectId) {
        if (!this.instance) throw new Error("Container not booted");
        
        let success = false;
        let attempts = 0; 

        while (!success && attempts < 3) {
            attempts++;
            logger("system", `Installing Dependencies (Attempt ${attempts})`);
            let errorLogs = ""; 

            const rmProcess = await this.instance.spawn('rm', ['-rf', 'node_modules', 'package-lock.json', 'pnpm-lock.yaml']);
            await rmProcess.exit; 

            const process = await this.instance.spawn('npx', [
                'pnpm', 
                'install', 
                '--shamefully-hoist'
            ]);
            
            process.output.pipeTo(new WritableStream({
                write(data) {
                    errorLogs += data; 
                    console.info("[PNPM]", data); 
                }
            }));

            const exitCode = await process.exit;
            
            if (exitCode !== 0) {
                logger("system", `⚠️ pnpm install failed (code ${exitCode}). Notifying AI Coder...`);
                
                if (!this.isFixing) {
                    const autoPrompt = `SYSTEM ALERT: package installation failed. Logs:\n<logs>\n${errorLogs.slice(-8000)}\n</logs>\nPlease review package.json for invalid packages or version conflicts and rewrite it.`;
                    await this._notifyCoder(projectId, autoPrompt);
                }

                logger("system", "Waiting for AI to apply fixes before retrying...");
                let waitTicks = 0;
                while (this.isFixing && waitTicks < 60) {
                    await new Promise(r => setTimeout(r, 1000));
                    waitTicks++;
                }
                await new Promise(r => setTimeout(r, 2000)); 
            } else {
                logger("system", "✅ Dependencies installed beautifully.");
                success = true;
            }
        }
    }

    async start(onReady, logger, projectId) {
        if (!this.instance) throw new Error("Container not booted");

        // 🛑 BULLETPROOF ENVIRONMENT INJECTION
        const envVars = {
            GORILLA_API_KEY: window.GORILLA_API_KEY || " not found! ", 
            VITE_GORILLA_AUTH_ID: window.GORILLA_AUTH_ID || "", 
        };

        const serverErrorTracker = this._createDebouncedLogger(logger, "Runtime/Server", projectId);

            // 🛑 Listen for Browser errors from our injected script
        window.addEventListener("message", (e) => {
            if (e.data && e.data.type === 'iframe_error') {
                // Filter out the empty "Console Error: {}" messages
                if (e.data.message !== "Console Error: {}") {
                    console.info("🔴 [BROWSER ERROR CAUGHT]", e.data.message);
                    serverErrorTracker.push(e.data.message);
                }
            }
        });

        // 🛑 Zombie Server Auto-Restarter
        const bootServer = async () => {
            logger("system", "🚀 Starting Dev Server...");
            this.shell = await this.instance.spawn('npm', ['run', 'dev'], { env: envVars });

            this.shell.output.pipeTo(new WritableStream({
                write(data) {
                    if (data.includes('ReferenceError') || 
                        data.includes('SyntaxError') || 
                        data.includes('Error:') || 
                        data.includes('ERR_') ||
                        data.includes('[ERROR]') || 
                        data.includes('Failed to resolve')) {
                        
                        serverErrorTracker.push(data);
                    }
                    console.info("[VM]", data);
                }
            }));

            this.shell.exit.then(async (code) => {
                if (code !== 0) {
                    serverErrorTracker.push(`[FATAL] Dev server crashed with code ${code}. Please fix the syntax or configuration errors.`);
                    serverErrorTracker.flushImmediate();
                    logger("system", "⚠️ Server crashed. Rebooting in 5s...");
                    
                    let waitTicks = 0;
                    while (this.isFixing && waitTicks < 30) {
                        await new Promise(r => setTimeout(r, 1000));
                        waitTicks++;
                    }
                    setTimeout(bootServer, 2000);
                }
            });
        };

        bootServer();

        this.instance.on('server-ready', (port, url) => {
            console.log(`⚡ Server ready at ${url}`);
            this.url = url;
            onReady(url);
        });
    }

    async writeFile(path, content) {
        if (!this.instance) return;
        await this.instance.fs.writeFile(path, content);
    }
}

export const webRunner = new WebRunner();