# Connect an AI client

After [initializing a project and attaching a PDF](independent-projects.md),
run `make web` and `make mcp` in separate terminals. The browser at
<http://127.0.0.1:8081/connect> shows connection details for the active project.
The MCP service listens on <http://127.0.0.1:8000/mcp> by default.

For Claude Code, run `make connect` in another terminal. Restart the client and
use `/mcp` to check the connection. `make reconnect` replaces that connection
and its token; `make disconnect` revokes and removes it. To connect a different
existing project, supply `CLIENT_PROJECT=/absolute/path/to/project` consistently.
These commands do not remove papers or evidence.

To check retrieval independently of a model, use a term present in your papers:

```sh
.venv/bin/istopmm-mcp-check \
  --url http://127.0.0.1:8000/mcp \
  --web-url http://127.0.0.1:8081 \
  --token-file releases/http-local/research-papers-claude-token \
  --collection core --query YOUR_SEARCH_TERM
```

The checker compares MCP context with the browser and JSON API. A connection
alone does not verify scientific content. Tokens are secrets; do not commit or
share them. Only connect services authorized to receive your research text.
The web server is intended for local use, not unauthenticated internet exposure.
