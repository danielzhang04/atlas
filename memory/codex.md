# Codex lessons

- 2026-08-27, Atlas Y3 status fixes: Put closed public status strings and the ASCII executable basename sanitizer in one shared module, then validate again at the HTTP boundary. This kept MCP and desktop details consistent and prevented path, query, fragment, argv, and exception text from escaping.
- What worked: A lazy immutable-style desktop snapshot on the health provider, plus a 600-second background refresh task owned by entrypoint shutdown, made repeated Settings polls side-effect free. A preflight live-task check in `McpServers.connect()` preserved the original task and child cleanup path on double connect.
- What failed first: The uncached health lambda re-ran signed resolution, resolution-stage exceptions all collapsed to `not_configured`, and a second connect overwrote the task dictionaries. The focused red run exposed all three before implementation.
- Evidence: 144 focused tests and 572 full tests passed; `node --check ui/app.js`, `git diff --check`, and CRLF checks passed. No remaining code decision is known for this round.
