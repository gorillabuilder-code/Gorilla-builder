
# Gor://a

![Gor://a](https://media.licdn.com/dms/image/v2/D4D3DAQGKJD52iDNcrA/image-scale_191_1128/B4DZui4YdXIEAc-/0/1767964248520/gorilla_builder_cover?e=1768572000&v=beta&t=6DrEkPwXetx7F24r1QflHlWVr7zuVhZOGrNA9tMTnNg)

gor://a (pronounced Gorilla) is an intelligent, browser-based development environment designed to accelerate rapid prototyping. It combines a traditional IDE interface with an autonomous AI agent loop that plans, codes, and deploys full-stack web applications in real-time.

Built with FastAPI, Supabase, and Groq, it features a persistent file system, live preview server, and a complete SaaS billing structure.

## Features

* Autonomous AI Agents:
* Planner: Breaks down prompts into actionable tasks and architectural plans.
* Coder: Executes the plan, writing code and managing file structures.


* Browser-based Editor: Full-featured CodeMirror editor with syntax highlighting and file tree navigation.
* Live Preview:
* Static: Instant preview for HTML/JS/CSS.
* Server: Integrated Uvicorn runner to preview backend Python logic directly in the browser.


* Cloud Persistence: All projects and files are synced real-time to Supabase Storage/DB.
* Token Economy: Built-in system for tracking token usage with Freemium, Premium ($12.99/mo), and Top-up billing flows.
* Dev Mode: Skip authentication hurdles during local development with a simulated user environment.

## Tech Stack

* Backend: Python, FastAPI, Uvicorn
* Database & Auth: Supabase (PostgreSQL)
* AI Inference: Groq API (LLM integration)
* Frontend: Vanilla JS, Jinja2 Templates, Server-Sent Events (SSE) for streaming logs.

## Getting Started

### Prerequisites

* Python 3.10+
* A Supabase project
* A Groq API Key

### Installation

1. Clone the repository
git clone [https://github.com/yourusername/gorilla.git](https://www.google.com/search?q=https://github.com/yourusername/gorilla.git)
cd gorilla
2. Install dependencies
pip install -r requirements.txt
3. Environment Setup
Create a .env file in the root directory:
# App Configuration


DEV_MODE=1
AUTH_SECRET_KEY=your_super_secret_key
MONTHLY_TOKEN_LIMIT=100000
# Database


SUPABASE_URL=[https://your-project.supabase.co](https://www.google.com/search?q=https://your-project.supabase.co)
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key
# AI


GROQ_API_KEY=gsk_your_groq_api_key
4. Database Schema
Run the SQL scripts located in /db/schema.sql in your Supabase SQL Editor.

### Running the App

Start the development server:

uvicorn app:app --reload

Visit http://localhost:8000 in your browser.

## Billing & Monetization

The application includes a mockup billing system located at /pricing.

* Premium Plan: $12.99/month logic.
* Token Top-up: Dynamic checkout for purchasing extra tokens ($1/100k).
* Limit Enforcement: The backend automatically halts agent execution if the user's tokens_used exceeds the MONTHLY_TOKEN_LIMIT.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details.