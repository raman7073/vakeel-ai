# How AI was used to build Vakeel AI

| Area | What we did |
|---|---|
| **Before the hackathon** | I built the core backend (September 2026) with help from Google Gemini |
| **Hackathon pair programmer** | Claude Code (Claude Opus 5.5) in VS Code, working directly in the repository |
| **Planning** | Reviewed the codebase first, made a gap list, then built each feature in small steps (schema, service, route, tests), running all tests after every step |
| **Context** | The AI read the real source, tests and docs. Existing code patterns served as the spec for new code. Secrets were always masked: `.env` values were shown only as `<hidden>` |
| **Agent and skills** | Claude Code used tools: terminal, Docker, live API calls, headless Chrome. Its PDF skill generated the demo contract |
| **Reusable AI instructions (in the product)** | Five prompt templates and a language instruction in `app/service/prompt.py`, sharing one set of guardrail rules |
| **Product guardrails** | Contract text treated as untrusted data, a strict JSON schema plus validation, server-side quote checking, model fallback |
| **Development guardrails** | No commits or pushes without approval. Secrets never displayed. An unknown program using port 5173 was left alone and the UI moved to 5180 |
| **Human review** | I set the priorities: backend features before the UI, Docker for MongoDB, a separate UI repository, a PDF for the demo. When the AI misread "remove this git" and staged changes in the wrong repository, I corrected it, and it reverted before anything was committed |
| **AI-assisted QA** | 20 backend and 13 UI tests, live Gemini end-to-end runs, and headless-browser screenshots reviewed and fixed. AI-found issues: model overload and daily-quota exhaustion (fixed with fallback), a damaged-PDF crash, unused prompts, and a misquote caught by the quote checker |
| **Skills practised** | Integrating LLMs with structured JSON output · prompt engineering and prompt-injection defence · checking AI answers against the source · fallback for model outages and quotas · testing AI systems · working with an AI coding agent while keeping a human in control |
