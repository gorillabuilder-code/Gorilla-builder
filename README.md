
<div align="center">

<pre>
   _____                  ____     ____        _ _     _           
  / ____|           _    / / /    |  _ \      (_) |   | |          
 | |  __  ___  _ __(_)  / / /_ _  | |_) |_   _ _| | __| | ___ _ __ 
 | | |_ |/ _ \| '__|   / / / _` | |  _ <| | | | | |/ _` |/ _ \ '__|
 | |__| | (_) | |   _ / / / (_| | | |_) | |_| | | | (_| |  __/ |   
  \_____|\___/|_|  (_)_/_/ \__,_| |____/ \__,_|_|_|\__,_|\___|_|   
  
</pre>

**The Autonomous Multi-Agent Orchestration Engine for Full-Stack SaaS.**
[![SPONSORED BY E2B FOR STARTUPS](https://img.shields.io/badge/SPONSORED%20BY-E2B%20FOR%20STARTUPS-ff8800?style=for-the-badge)](https://e2b.dev/startups)
[![Discord](https://img.shields.io/badge/Discord-Join_the_Beta-7289da?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/V3f3PkwQbY)
[![Website](https://img.shields.io/badge/Website-gorillabuilder.dev-00ff41?style=for-the-badge)](https://gorillabuilder.dev)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-GorillaBuilder-blue?style=for-the-badge&logo=linkedin)](https://www.linkedin.com/in/gorillabuilder)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)

</div>

---

## 🦍 Stop Building Wrappers. Build Engines.

The current "VybeCoding" ecosystem is a trap. AI tools generate decent React UIs but leave developers completely stranded when it comes to configuring databases, cloud secrets, authentication, and deployment pipelines. 

**Gorilla Builder** is an open-source, 22,000-line multi-agent orchestration engine. It doesn't just generate code snippets; it autonomously generates, syncs, and deploys production-ready React and Node.js applications while completely eliminating backend boilerplate.

We built this so you can launch a production-ready SaaS with **zero API keys or cloud secrets required**.

## ⚡ The Technical Moat

Gorilla Builder is not a thin wrapper over the OpenAI API. It is a distributed infrastructure engine.

* **Unified AI & Auth Gateway:** A provider-agnostic edge layer that natively injects live OAuth (Google Sign-In) and AI capabilities (Chat, Image Gen) directly into the generated app. 
* **Browser-Native Execution (WebContainers):** We run a live, headless Node.js environment directly inside the user's browser. This handles real-time dependency resolution and file-system operations with zero server-side overhead or Docker orchestration.
* **Swarm Choreography (MCP):** Powered by the Model Context Protocol, Gorilla Builder uses a swarm of specialized Python agents (`Planner` -> `Coder` -> `Reviewer`). They operate in isolated contexts, passing structured JSON states to prevent hallucination cascades.
* **AST Metaprogramming:** Instead of blindly rewriting entire files, our agents utilize Abstract Syntax Tree (AST) parsing to autonomously traverse, mutate, and inject code deep into existing directories without breaking syntactic validity.
* **Zero-Config CI/CD:** Native integrations with GitHub and Vercel. Go from a text prompt (or a Figma URL) to a live, deployed SaaS URL in one click.


## 🤝 Join the Swarm (Private Beta)

We are actively recruiting systems engineers, AI architects, and hackers to push the limits of this engine. We need help optimizing AST parsers, improving the WebContainer memory footprint, and expanding the AgentSkills via MCP.

**Private Beta & 2.5M Tokens:**
The hosted platform operates on an ad-subsidized free tier (500k tokens) and a $13.99 Pro tier (5 million tokens). 

If you contribute to the core engine or join our beta testing cohort, we will upgrade your hosted Gorilla Builder account to a **2.5 MILLION token Premium Beta role**. 

Join the [Discord](https://discord.gg/V3f3PkwQbY) to claim your role, report bugs, and give feedback.

## 🔗 Links

- **Website:** [gorillabuilder.dev](https://gorillabuilder.dev)
- **Discord:** [Join the Beta](https://discord.gg/V3f3PkwQbY)
- **LinkedIn:** [GorillaBuilder](https://www.linkedin.com/in/gorillabuilder)
- **GitHub:** [Gorilla-builder](https://github.com/GorillaBuilder/Gorilla-builder)

## 🛡️ License

Distributed under the MIT License. See `LICENSE` for more information.
