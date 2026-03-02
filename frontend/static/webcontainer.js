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
    }

    /**
     * Boot the WebContainer. Must be called once.
     */
    async boot() {
        if (this.instance) return this.instance;
        
        console.log("Booting WebContainer...");
        
        // We must tell WebContainer that our server uses 'credentialless' headers.
        this.instance = await WebContainer.boot({
            coep: 'credentialless' 
        });

        return this.instance;
    }

    /**
     * Convert flat database files to WebContainer tree format
     */
    _convertFilesToTree(files) {
        const tree = {};
        
        files.forEach(f => {
            const cleanPath = f.path.replace(/^\/+/, '');
            const parts = cleanPath.split('/'); 
            let current = tree;
            
            parts.forEach((part, index) => {
                const isFile = index === parts.length - 1;
                
                if (isFile) {
                    current[part] = { file: { contents: f.content || "" } };
                } else {
                    if (!current[part]) {
                        current[part] = { directory: {} };
                    }
                    current = current[part].directory;
                }
            });
        });
        
        return tree;
    }

    /**
     * Mount files into the virtual file system
     */
    async mount(flatFiles) {
        if (!this.instance) await this.boot();
        const tree = this._convertFilesToTree(flatFiles);
        await this.instance.mount(tree);
        console.log("📂 Files mounted into Browser VM");
    }

    /**
     * Helper to create a debounced error logger for Runtime errors
     */
    _createDebouncedLogger(logger, contextName, projectId) {
        let buffer = "";
        let timeout = null;

        const flush = () => {
            if (buffer.trim() !== "") {
                const prompt = `SYSTEM ALERT: ❌ ${contextName} Runtime Errors:\n<logs>\n${buffer.trim()}\n</logs>\nPlease fix these issues in the codebase.`;
                logger("coder", prompt); // Show in UI
                this._notifyCoder(projectId, prompt); // Send to backend
                buffer = "";
            }
        };

        return {
            push: (data) => {
                buffer += data + "\n";
                if (timeout) clearTimeout(timeout);
                timeout = setTimeout(flush, 4000); // Wait 4s for cascade of errors to finish
            },
            flushImmediate: () => {
                if (timeout) clearTimeout(timeout);
                flush();
            }
        };
    }

    /**
     * 🚨 NEW: Securely dispatch error logs to the AI Agent Backend
     */
    async _notifyCoder(projectId, systemPrompt) {
        // Fallback to extract projectId from URL if not explicitly passed
        const pid = projectId || window.PROJECT_ID || window.location.pathname.split('/')[2];
        
        if (!pid) {
            console.warn("No Project ID found. Cannot auto-notify coder.");
            return;
        }

        try {
            await fetch(`/api/project/${pid}/chat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ prompt: systemPrompt, isSystemMessage: true })
            });
            console.log("🤖 Logs successfully dispatched to AI Coder.");
        } catch (err) {
            console.error("Failed to reach AI Coder:", err);
        }
    }

    /**
     * Run npm install (With Retry Loop, Legacy Peer Deps & Auto-Heal)
     */
    async install(logger, projectId) {
        if (!this.instance) throw new Error("Container not booted");
        
        let success = false;

        while (!success) {
            logger("system", "Installing dependencies...");
            let errorLogs = ""; 

            // 🚨 Use legacy-peer-deps to ignore strict React versioning conflicts
            const process = await this.instance.spawn('npm', ['install', '--legacy-peer-deps']);
            
            process.output.pipeTo(new WritableStream({
                write(data) {
                    errorLogs += data; // Capture full output for the AI
                    if(data.includes('ERR') || data.includes('warn')) {
                        console.warn("[NPM]", data);
                    }
                }
            }));

            const exitCode = await process.exit;
            
            if (exitCode !== 0) {
                logger("system", `⚠️ npm install failed (code ${exitCode}). Notifying AI Coder...`);
                
                // Grab the last 1500 chars so we don't blow up the AI token context limit
                const truncatedLogs = errorLogs.slice(-1500);
                const autoPrompt = `SYSTEM ALERT: The WebContainer failed to boot. \`npm install --legacy-peer-deps\` exited with code ${exitCode}. \nHere are the logs:\n<logs>\n${truncatedLogs}\n</logs>\nPlease review the package.json. Fix any invalid package names, version conflicts, or missing dependencies, and rewrite the package.json file.`;

                // Fire the API call!
                await this._notifyCoder(projectId, autoPrompt);
                
                logger("system", "Coder notified! Waiting for fix before retrying in 10s...");
                await new Promise(resolve => setTimeout(resolve, 10000));
            } else {
                logger("system", "✅ Dependencies installed.");
                success = true;
            }
        }
    }

    /**
     * Start the Dev Server & Push DB (With Auto-Heal loops)
     */
    async start(onReady, logger, projectId) {
        if (!this.instance) throw new Error("Container not booted");

        const envVars = {
            GORILLA_API_KEY: window.GORILLA_API_KEY || "", 
        };

        // 🚨 DATABASE PUSH WITH AUTO-HEAL LOOP 🚨
        let dbSuccess = false;
        
        while (!dbSuccess) {
            logger("system", "🗄️ Provisioning local SQLite database...");
            let dbErrorLogs = "";

            const dbProcess = await this.instance.spawn('npm', ['run', 'db:push'], { env: envVars });
            
            dbProcess.output.pipeTo(new WritableStream({
                write(data) {
                    dbErrorLogs += data;
                    console.log("[DB PUSH]", data);
                }
            }));

            const dbExitCode = await dbProcess.exit;
            
            if (dbExitCode !== 0) {
                logger("system", `⚠️ Database push failed (code ${dbExitCode}). Notifying AI Coder...`);
                
                const truncatedLogs = dbErrorLogs.slice(-1500);
                const autoPrompt = `SYSTEM ALERT: \`npm run db:push\` failed with code ${dbExitCode}. \nHere are the logs:\n<logs>\n${truncatedLogs}\n</logs>\nPlease review the Drizzle schema and connection settings, fix the errors, and rewrite the files.`;

                await this._notifyCoder(projectId, autoPrompt);

                logger("system", "Coder notified! Retrying DB push in 10s...");
                await new Promise(resolve => setTimeout(resolve, 10000));
            } else {
                logger("system", "✅ Database created successfully!");
                dbSuccess = true;
            }
        }

        // 🚀 START SERVER AS NORMAL
        logger("system", "🚀 Starting Dev Server...");

        this.shell = await this.instance.spawn('npm', ['run', 'dev'], { env: envVars });

        const serverErrorTracker = this._createDebouncedLogger(logger, "Runtime/Server", projectId);

        this.shell.output.pipeTo(new WritableStream({
            write(data) {
                if (data.includes('ReferenceError') || 
                    data.includes('SyntaxError') || 
                    data.includes('Error:') || 
                    data.includes('[ERROR]') || 
                    data.includes('Failed to resolve')) {
                    
                    serverErrorTracker.push(data);
                }
                console.log("[VM]", data);
            }
        }));

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