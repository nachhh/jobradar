# Diagrams

Written in [Mermaid](https://mermaid.js.org/): diagrams as text inside Markdown, so they are versioned and reviewed like code. GitHub renders them on the page; in VS Code, open the Markdown preview (`Cmd+Shift+V`) with the *Markdown Preview Mermaid Support* extension.

| Diagram | Shows |
|---|---|
| [Architecture](architecture.md) | Components and where they run: job APIs, LLM APIs, CLI, pipelines, database |
| [Data flow](data-flow.md) | How a posting travels from an API response to `current_postings`, and through LLM enrichment |
| [Entity-relationship](erd.md) | Tables, columns and relationships |
| [State machines](state-machines.md) | Lifecycles of a source within a run, a run, a posting, and its enrichment |

They describe what is built, not plans: each phase updates the diagrams it changes.
