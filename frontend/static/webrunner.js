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
                    current[part] = { file: { contents: f.content || "" } };
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
            if (buffer.trim() !== "" && !this.isFixing) {
                const prompt = `SYSTEM ALERT: ❌ ${contextName} Runtime Errors:\n<logs>\n${buffer.trim()}\n</logs>\nPlease fix these issues in the codebase.`;
                logger("coder", prompt); 
                this._notifyCoder(projectId, prompt); 
                buffer = "";
            }
        };

        return {
            push: (data) => {
                buffer += data + "\n";
                if (timeout) clearTimeout(timeout);
                timeout = setTimeout(flush, 4000); 
            },
            flushImmediate: () => {
                if (timeout) clearTimeout(timeout);
                flush();
            }
        };
    }

    async _notifyCoder(projectId, systemPrompt) {
        if (this.isFixing) {
            console.info("⏳ AI is already working on a fix. Ignoring duplicate error.");
            return;
        }

        this.isFixing = true;
        const pid = projectId || window.PROJECT_ID || (window.location.pathname.match(/\/projects\/([^\/]+)/) || [])[1];
        
        if (!pid) {
            console.info("No Project ID found. Cannot auto-notify coder.");
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
            this.fixUnlockTimer = setTimeout(() => { 
                this.isFixing = false; 
                console.info("🔓 AI Fix Lock released.");
            }, 30000); 
        }
    }

    async install(logger, projectId) {
        if (!this.instance) throw new Error("Container not booted");
        
        let success = false;
        let attempts = 0; 

        while (!success && attempts < 3) {
            attempts++;
            logger("system", `🚀 Firing NPM, Hiring PNPM... (Attempt ${attempts})`);
            let errorLogs = ""; 

            // 🛑 Clear the board completely (Added pnpm-lock.yaml)
            const rmProcess = await this.instance.spawn('rm', ['-rf', 'node_modules', 'package-lock.json', 'pnpm-lock.yaml']);
            await rmProcess.exit; 

            // 🛑 THE ULTIMATE BYPASS: Use pnpm instead of npm.
            // --shamefully-hoist ensures standard npm run scripts still work natively.
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
                logger("system", `⚠️ pnpm install failed. Retrying in 5s...`);
                await new Promise(resolve => setTimeout(resolve, 5000));
            } else {
                logger("system", "✅ Dependencies installed beautifully.");
                success = true;
            }
        }
        
        if (!success) {
            logger("system", "❌ Critical Failure: Notifying AI Coder to check package.json...");
            const autoPrompt = `SYSTEM ALERT: package installation failed 3 times. Please carefully review package.json for invalid package names or missing brackets and rewrite it.`;
            await this._notifyCoder(projectId, autoPrompt);
        }
    }

    async start(onReady, logger, projectId) {
        if (!this.instance) throw new Error("Container not booted");

        const envVars = {
            GORILLA_API_KEY: window.GORILLA_API_KEY || "", 
        };

        let dbSuccess = false;
        let dbAttempts = 0;
        
        while (!dbSuccess && dbAttempts < 3) {
            dbAttempts++;
            logger("system", `🗄️ Provisioning local SQLite database... (Attempt ${dbAttempts})`);
            let dbErrorLogs = "";

            const dbProcess = await this.instance.spawn('npm', ['run', 'db:push'], { env: envVars });
            
            dbProcess.output.pipeTo(new WritableStream({
                write(data) {
                    dbErrorLogs += data;
                    console.info("[DB PUSH]", data);
                }
            }));

            const dbExitCode = await dbProcess.exit;
            
            if (dbExitCode !== 0) {
                logger("system", `⚠️ Database push failed (code ${dbExitCode}). Notifying AI Coder...`);
                
                const truncatedLogs = dbErrorLogs.slice(-1500);
                const autoPrompt = `SYSTEM ALERT: \`npm run db:push\` failed with code ${dbExitCode}. \nHere are the logs:\n<logs>\n${truncatedLogs}\n</logs>\nPlease review the Drizzle schema and connection settings, fix the errors, and rewrite the files.`;

                await this._notifyCoder(projectId, autoPrompt);

                logger("system", "Coder notified! Retrying DB push in 25s...");
                await new Promise(resolve => setTimeout(resolve, 25000));
            } else {
                logger("system", "✅ Database created successfully!");
                dbSuccess = true;
            }
        }

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
                console.info("[VM]", data);
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