# Atlas browser bridge (MV3)

This extension is intended for a dedicated Chrome profile. It has no externally-connectable
surface, no broad host permission, no cookies/debugger/webRequest/history permission, and does not
run in incognito. A future packaging step may configure exact canonical origins in the profile;
the shipped default is an empty allowlist and therefore fails closed.

The service worker accepts only typed messages from its own content script and revalidates the
tab's canonical origin and extension-issued document ID before every operation. Visible text is
bounded and returned as untrusted evidence. Navigation is allowed only when both the requested
origin and the final redirect origin are exact members of the allowlist.

This tranche intentionally does not register a native host, install a registry entry, launch
Chrome, or expose a network endpoint.

The current repository has no shared browser MCP/capability transport to attach to. The typed
Python protocol in `atlas/worker/browser_protocol.py` is deliberately transport-agnostic so the
same service can later be called by Claude CLI and Atlas; this extension does not create a second
Atlas-only credential or permission store.
