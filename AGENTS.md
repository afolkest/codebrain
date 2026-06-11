# Agent instructions

- Do not infer product/session semantics from free-text prompt regexes. For retrieval filters and classification, use structured signals first: schema columns, source JSON fields, tool names/ids, parent/session lineage, refs, and adapter-emitted metadata.
- Text-prefix filters are acceptable only for explicit source-generated control markup used for display cleanup, not for classifying sessions, user intent, subagents, branches, or preferences.
- When adding a filter, include a test that proves the structured signal drives the behavior.
