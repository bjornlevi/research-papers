# Connect an AI client

After [initializing a project and attaching a PDF](independent-projects.md),
run `make web` and `make mcp`. The website starts detached; open
<http://127.0.0.1:8081/connect> for connection details and use `make web-off`
to stop it. Keep the MCP service running in its terminal. It listens on
<http://127.0.0.1:8000/mcp> by default.

For Claude Code, run `make connect` in another terminal. Restart the client and
use `/mcp` to check the connection. `make reconnect` replaces that connection
and its token; `make disconnect` revokes and removes it. To connect a different
existing project, supply `CLIENT_PROJECT=/absolute/path/to/project` consistently.
These commands do not remove papers or evidence.

`PROJECT_ROOT` selects the research data (default: `releases/project`).
`make mcp PROJECT_ROOT=/path/to/project` uses that project's `registry/`
unless `COLLECTION_REGISTRY` is explicitly overridden. The website's connection
page provides a start command with its configured registry, collections and
profile. Use it when serving a non-default project.

`CLIENT_PROJECT` is different: it selects the existing directory where the
Claude Code connection is registered, not the research data being served.
`make connect` registers `HTTP_URL` and a private credential; it does not select
or start an evidence collection. The default connection name is
`research-evidence-http`. `CLIENT_NAME` can override it; use the same
name and credential settings for reconnect and disconnect.
Existing registrations under another name are not renamed or removed
automatically; pass their original `CLIENT_NAME` when managing them.

To check retrieval independently of a model, use a term present in your papers:

```sh
.venv/bin/research-mcp-check \
  --url http://127.0.0.1:8000/mcp \
  --web-url http://127.0.0.1:8081 \
  --token-file releases/http-local/research-papers-claude-token \
  --collection core --query YOUR_SEARCH_TERM
```

The checker compares MCP context with the browser and JSON API. A connection
alone does not verify scientific content. Tokens are secrets; do not commit or
share them. Only connect services authorized to receive your research text.
The web server is intended for local use, not unauthenticated internet exposure.
