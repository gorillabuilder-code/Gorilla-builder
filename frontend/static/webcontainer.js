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
        
        console.log("🥾 Booting WebContainer (Credentialless Mode)...");
        
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
     * Start the Dev Server
     */
    async start(onReady, logger) {
        if (!this.instance) throw new Error("Container not booted");

        logger("system", "🚀 Starting Dev Server...");

        // Run 'npm run dev'
        this.shell = await this.instance.spawn('npm', ['run', 'dev']);

        this.shell.output.pipeTo(new WritableStream({
            write(data) {
                if (data.includes('ReferenceError') || data.includes('SyntaxError')) {
                    logger("coder", `❌ Runtime Error:\n${data}`);
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