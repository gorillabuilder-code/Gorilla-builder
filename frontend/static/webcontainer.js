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
        
        // 🚨 THIS IS THE CRITICAL FIX 🚨
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
     * Run npm install
     */
    async install(logger) {
        if (!this.instance) throw new Error("Container not booted");
        
        logger("system", "Installing dependencies...");
        
        // Use 'npm install'
        const process = await this.instance.spawn('npm', ['install']);
        
        process.output.pipeTo(new WritableStream({
            write(data) {
                if(data.includes('ERR') || data.includes('warn')) console.warn(data);
            }
        }));

        const exitCode = await process.exit;
        if (exitCode !== 0) {
            logger("system", `⚠️ npm install finished with code ${exitCode}`);
        } else {
            logger("system", "✅ Dependencies installed.");
        }
    }

    /**
     * Start the Dev Server (AND Push DB First)
     */
    async start(onReady, logger) {
        if (!this.instance) throw new Error("Container not booted");

        // 🚨 NEW DATABASE PUSH STEP 🚨
        logger("system", "🗄️ Provisioning local SQLite database...");
        
        const dbProcess = await this.instance.spawn('npm', ['run', 'db:push']);
        
        dbProcess.output.pipeTo(new WritableStream({
            write(data) {
                console.log("[DB PUSH]", data); // Logs to browser console so you can debug
            }
        }));

        const dbExitCode = await dbProcess.exit;
        if (dbExitCode !== 0) {
            logger("system", `⚠️ Database push failed (code ${dbExitCode}). Check console.`);
        } else {
            logger("system", "✅ Database created successfully!");
        }

        // 🚀 START SERVER AS NORMAL
        logger("system", "🚀 Starting Dev Server...");

        // Run 'npm run dev'
        this.shell = await this.instance.spawn('npm', ['run', 'dev']);

        // 🚨 ERROR AGGREGATION BUFFER 🚨
        let errorBuffer = "";

        // Flush the buffer to the coder every 3 seconds
        const errorInterval = setInterval(() => {
            if (errorBuffer.trim() !== "") {
                logger("coder", `❌ Compiled Runtime Errors:\n${errorBuffer.trim()}`);
                errorBuffer = ""; // Reset the buffer after sending
            }
        }, 3000);

        this.shell.output.pipeTo(new WritableStream({
            write(data) {
                // Catch standard Node errors and Vite build errors
                if (data.includes('ReferenceError') || 
                    data.includes('SyntaxError') || 
                    data.includes('Error:') || 
                    data.includes('[ERROR]') || 
                    data.includes('Failed to resolve')) {
                    
                    // Add the chunk to our buffer instead of sending immediately
                    errorBuffer += data + "\n";
                }
                console.log("[VM]", data);
            }
        }));

        // Listen for the "Server Ready" event
        this.instance.on('server-ready', (port, url) => {
            console.log(`⚡ Server ready at ${url}`);
            this.url = url;
            onReady(url);
        });
    }

    /**
     * Write a single file (Hot Module Reload trigger)
     */
    async writeFile(path, content) {
        if (!this.instance) return;
        await this.instance.fs.writeFile(path, content);
    }
}

export const webRunner = new WebRunner();