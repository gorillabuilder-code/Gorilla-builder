import { WebContainer } from 'https://esm.sh/@webcontainer/api@1.1.8';

/**
 * WebContainer Orchestrator
 * Handles the browser-based Node.js runtime.
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
     * Helper to create a debounced error logger
     */
    _createDebouncedLogger(logger, contextName) {
        let buffer = "";
        let timeout = null;

        const flush = () => {
            if (buffer.trim() !== "") {
                logger("coder", `❌ ${contextName} Errors:\n${buffer.trim()}\n\nPlease fix these issues.`);
                buffer = "";
            }
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

    /**
     * Run npm install (With Retry Loop & Error Debouncing)
     */
    async install(logger) {
        if (!this.instance) throw new Error("Container not booted");
        
        let success = false;

        while (!success) {
            logger("system", "Installing dependencies...");
            const errorTracker = this._createDebouncedLogger(logger, "NPM Install");

            const process = await this.instance.spawn('npm', ['install']);
            
            process.output.pipeTo(new WritableStream({
                write(data) {
                    if(data.includes('ERR') || data.includes('warn')) {
                        console.warn("[NPM]", data);
                        errorTracker.push(data);
                    }
                }
            }));

            const exitCode = await process.exit;
            
            if (exitCode !== 0) {
                errorTracker.flushImmediate();
                logger("system", `⚠️ npm install failed (code ${exitCode}). Coder notified. Retrying in 10s...`);
                await new Promise(resolve => setTimeout(resolve, 10000));
            } else {
                logger("system", "✅ Dependencies installed.");
                success = true;
            }
        }
    }

    /**
     * Start the Dev Server (AND Push DB First, with retry loops)
     */
    async start(onReady, logger) {
        if (!this.instance) throw new Error("Container not booted");

        // 🚨 PHASE 4: Inject the Gorilla API Key into the environment
        const envVars = {
            GORILLA_API_KEY: window.GORILLA_API_KEY || "", 
        };

        // 🚨 DATABASE PUSH WITH RETRY LOOP 🚨
        let dbSuccess = false;
        
        while (!dbSuccess) {
            logger("system", "🗄️ Provisioning local SQLite database...");
            const dbErrorTracker = this._createDebouncedLogger(logger, "Database Push");

            // Pass envVars to the DB push in case any seed scripts need the AI
            const dbProcess = await this.instance.spawn('npm', ['run', 'db:push'], { env: envVars });
            
            dbProcess.output.pipeTo(new WritableStream({
                write(data) {
                    console.log("[DB PUSH]", data);
                    if (data.toLowerCase().includes('error') || data.includes('ERR')) {
                        dbErrorTracker.push(data);
                    }
                }
            }));

            const dbExitCode = await dbProcess.exit;
            
            if (dbExitCode !== 0) {
                dbErrorTracker.flushImmediate();
                logger("system", `⚠️ Database push failed (code ${dbExitCode}). Coder notified. Retrying in 10s...`);
                await new Promise(resolve => setTimeout(resolve, 10000));
            } else {
                logger("system", "✅ Database created successfully!");
                dbSuccess = true;
            }
        }

        // 🚀 START SERVER AS NORMAL
        logger("system", "🚀 Starting Dev Server...");

        // 🚨 PHASE 4: Pass envVars to the dev server so the deployed Node app has the key!
        this.shell = await this.instance.spawn('npm', ['run', 'dev'], { env: envVars });

        const serverErrorTracker = this._createDebouncedLogger(logger, "Runtime/Server");

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