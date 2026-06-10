# Style Guide

## Product Style

Maestro should feel like a calm operational cockpit: direct, useful, and built for repeated daily use. Avoid marketing-page patterns inside the app. Prioritize dense but readable information, fast scanning, and clear action paths.

## Backend

- Prefer typed Python.
- Keep domain boundaries explicit.
- Put routing/API concerns in `app/api`.
- Put orchestration and shared runtime behavior in `app/core`.
- Put domain agents in `app/agents` or `app/domains` depending on how implementation shakes out.
- Put memory-specific behavior in `app/memory`.
- Keep repository/database access behind small service or repository modules.

## Frontend

- Build the actual Maestro interface first, not a landing page.
- Make phone usability a first-order requirement.
- Use clear navigation: Maestro chat, reports, domains, agents, tools, and settings.
- Use restrained visual styling suitable for an operational tool.
- Keep controls familiar: icons for repeated tool actions, toggles for binary settings, menus for option sets, and tabs when switching views.

## Docs

- Keep docs close to decisions.
- Update architecture docs when behavior or boundaries change.
- Use Mermaid diagrams for system flows where helpful.
- Keep issue descriptions implementation-ready with acceptance criteria.

## Tests

- Add focused tests for shared logic, persistence, memory scoping, and workflow orchestration.
- Broaden tests when a change touches domain boundaries or user-facing workflow behavior.
