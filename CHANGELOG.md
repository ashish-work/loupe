# Changelog

## 1.0.0

Initial public release.

- Interactive, human-in-the-loop PR review UI (accept / edit / dismiss findings, add line comments, pick a verdict).
- Severity-tagged, line-anchored findings authored by the agent from the raw diff.
- UML **structure** map for type-shape changes (added / modified / removed classes + members + relationships), clickable into the diff.
- **Overview diagrams** (Mermaid): architecture overlay, capability map, and user-journey flow rendered above the diff; the agent asks which to include. Falls back to source when offline.
- Curated review returned as JSON for posting a GitHub review or summarizing.
