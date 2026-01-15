
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
* Token Economy: Built-in system for tracking token usage with Freemium, Premium ($13.99/mo), and Top-up billing flows.
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

4. Database Schema
Run the SQL scripts located in /db/schema.sql in your Supabase SQL Editor.

### Running the App

Start the development server:

uvicorn app:app --reload

Visit http://localhost:8000 in your browser.

## Billing & Monetization

The application includes a mockup billing system located at /pricing.

* Premium Plan: $13.99/month logic.
* Token Top-up: Dynamic checkout for purchasing extra tokens ($1/100k).
* Limit Enforcement: The backend automatically halts agent execution if the user's tokens_used exceeds the MONTHLY_TOKEN_LIMIT.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

