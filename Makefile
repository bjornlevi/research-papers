PYTHON ?= python3
VENV ?= .venv
PY := $(VENV)/bin/python
RELEASE ?= releases/local-vscode-demo-001
PROFILE ?= private_context
-include local.mk
HTTP_PORT ?= 8000
LAN_HOST ?=
HTTP_URL ?= http://$(or $(LAN_HOST),127.0.0.1):$(HTTP_PORT)/mcp
MCP_BIND ?= $(LAN_HOST)
HTTP_BIND = $(if $(LAN_HOST),--host "$(MCP_BIND)" --allow-plain-lan)
LAN_TOKENS ?= releases/http-local/lan
WEB_PORT ?= 8081
WEB_STATE_DIR ?= releases
TOKEN_REGISTRY ?= releases/http-local/tokens.json
TOKEN_FILE ?= releases/http-local/client-token
CONTAINER_ENGINE ?= podman
IMAGE ?= localhost/research-library:m5
PROJECT_ROOT ?= releases/project
COLLECTION_REGISTRY ?= $(PROJECT_ROOT)/registry
COLLECTIONS ?= core
DEFAULT_COLLECTIONS ?= core
COLLECTION_ARGS = --registry "$(COLLECTION_REGISTRY)" $(foreach c,$(COLLECTIONS),--allow-collection "$(c)") $(foreach c,$(DEFAULT_COLLECTIONS),--default-collection "$(c)")
PROJECT_NAME ?=
PROJECT_AUTHORIZATION ?=
PROJECT_PAPERS ?=
PDF_DATA_ROOT ?= releases/pdf-data
CLIENT_PROJECT ?= .
CLIENT_NAME ?= research-evidence-http
CLIENT_TOKEN_FILE ?= releases/http-local/research-papers-claude-token
CLIENT_ARGS = --project "$(CLIENT_PROJECT)" --name "$(CLIENT_NAME)" --registry "$(TOKEN_REGISTRY)" --token-file "$(CLIENT_TOKEN_FILE)" --url "$(HTTP_URL)" --profile "$(PROFILE)"

.PHONY: help setup project-init project-status run web web-off mcp http-token lan-token lan-revoke share cloudflared connect connect-claude disconnect reconnect test-http test-connection test-collections test-catalog test-citations test-project container-build test-container test evaluate test-install check

help:
	@printf '%s\n' \
		'make setup            Create the virtualenv and install development dependencies' \
		'make project-init PROJECT_NAME=… PROJECT_AUTHORIZATION=… PROJECT_PAPERS=…  Initialize your private project' \
		'make project-status  Show the current project status (PROJECT_ROOT=… to select another project)' \
		'make run              Start the local stdio MCP server (not an interactive chat)' \
		'make http-token       Issue a private local HTTP client token (once; expires in 24h)' \
		'make web              Start the detached local website on port $(WEB_PORT)' \
		'make web-off          Stop the detached local website' \
		'make mcp              Start the authenticated HTTP MCP server on port 8000 (LAN_HOST=ip: on the LAN; MCP_BIND=0.0.0.0: all interfaces)' \
		'make lan-token NAME=… Issue a 30-day token for another LAN computer (needs LAN_HOST)' \
		'make lan-revoke NAME=…  Revoke that computer'"'"'s token' \
		'make share            Share MCP with web AI apps (Claude/ChatGPT) via a temporary secret HTTPS link' \
		'make cloudflared      Download the official cloudflared binary into the virtualenv' \
		'make connect          Connect Claude Code in this project (issue/refresh private token)' \
		'make disconnect       Remove that local connection and revoke its token' \
		'make reconnect        Replace that connection with a fresh token' \
		'make connect-claude   Alias for make connect; CLIENT_PROJECT overrides the project' \
		'make test-http        Run HTTP smoke, four-client load and rollback acceptance' \
		'make test-connection  Verify synthetic MCP search/context and browser-reader agreement' \
		'make test-collections Run multi-collection stdio/HTTP acceptance with synthetic fixtures' \
		'make test-catalog     Verify metadata catalog and evidence isolation over stdio/HTTP' \
		'make test-citations   Run fictional one-hop citation and licensed-PDF workflow tests' \
		'make test-project     Test independent project ingestion and citation discovery' \
		'make container-build  Build the pinned non-root container with Podman' \
		'make test-container   Verify read-only container, HTTP access and restart' \
		'make check            Run portable tests, MCP evaluation and isolated install checks' \
		'make test             Run portable tests only' \
		'make evaluate         Run fictional MCP benchmark and research-card acceptance' \
		'make test-install     Verify isolated wheel/sdist installation' \
		'Project data and credentials stay local and out of Git.'

setup:
	$(PYTHON) -m venv "$(VENV)"
	"$(PY)" -m pip install -e '.[build,dev]'

project-init:
	@test -n "$(PROJECT_NAME)" || { echo 'Set PROJECT_NAME="My research project"'; exit 2; }
	@test -n "$(PROJECT_AUTHORIZATION)" || { echo 'Set PROJECT_AUTHORIZATION to your basis for private extraction and retrieval'; exit 2; }
	@test -n "$(PROJECT_PAPERS)" || { echo 'Set PROJECT_PAPERS to a CSV containing doi and title columns'; exit 2; }
	@"$(VENV)/bin/research-project" --root "$(PROJECT_ROOT)" init --name "$(PROJECT_NAME)" --authorization "$(PROJECT_AUTHORIZATION)" --papers "$(PROJECT_PAPERS)"

project-status:
	@"$(VENV)/bin/research-project" --root "$(PROJECT_ROOT)" status

test:
	"$(PY)" tests/run.py portable

run:
	@"$(VENV)/bin/research-mcp" $(if $(COLLECTION_REGISTRY),$(COLLECTION_ARGS),--release "$(RELEASE)") --allow-profile "$(PROFILE)"

http-token:
	@"$(VENV)/bin/research-http-token" issue --registry "$(TOKEN_REGISTRY)" --token-file "$(TOKEN_FILE)" --audience "$(HTTP_URL)" --profile "$(PROFILE)"

web:
	@"$(PY)" -m research_library.web_lifecycle start --registry "$(COLLECTION_REGISTRY)" --project-root "$(PROJECT_ROOT)" --pdf-data-root "$(PDF_DATA_ROOT)" --state-dir "$(WEB_STATE_DIR)" --allow-profile "$(PROFILE)" $(foreach c,$(COLLECTIONS),--allow-collection "$(c)") $(foreach c,$(DEFAULT_COLLECTIONS),--default-collection "$(c)") --port "$(WEB_PORT)" --mcp-url "$(HTTP_URL)"

web-off:
	@"$(PY)" -m research_library.web_lifecycle stop --registry "$(COLLECTION_REGISTRY)" --project-root "$(PROJECT_ROOT)" --state-dir "$(WEB_STATE_DIR)" --allow-profile "$(PROFILE)" $(foreach c,$(COLLECTIONS),--allow-collection "$(c)") $(foreach c,$(DEFAULT_COLLECTIONS),--default-collection "$(c)") --port "$(WEB_PORT)" --mcp-url "$(HTTP_URL)"

mcp:
	@"$(PY)" -m research_library.local_client init --registry "$(TOKEN_REGISTRY)"
	@"$(VENV)/bin/research-http" $(if $(COLLECTION_REGISTRY),$(COLLECTION_ARGS),--release "$(RELEASE)") $(foreach p,$(PROFILE),--allow-profile "$(p)") --token-registry "$(TOKEN_REGISTRY)" --port "$(HTTP_PORT)" --base-url "$(HTTP_URL)" $(HTTP_BIND)

lan-token:
	@test -n "$(LAN_HOST)" || { echo 'Set LAN_HOST (in local.mk or on the command line), e.g. LAN_HOST=192.168.1.10'; exit 2; }
	@echo "$(NAME)" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_-]*$$' || { echo 'Usage: make lan-token NAME=computer-name'; exit 2; }
	@"$(PY)" -m research_library.local_client init --registry "$(TOKEN_REGISTRY)"
	@"$(VENV)/bin/research-http-token" issue --registry "$(TOKEN_REGISTRY)" --token-file "$(LAN_TOKENS)/$(NAME)-token" --audience "$(HTTP_URL)" --profile "$(PROFILE)" --expires-in 2592000 >/dev/null
	@echo "Token for $(NAME) (valid 30 days) saved in $(LAN_TOKENS)/$(NAME)-token. On that computer run:"
	@echo
	@echo "  claude mcp add --transport http --scope user research-library $(HTTP_URL) --header \"Authorization: Bearer $$(cat '$(LAN_TOKENS)/$(NAME)-token')\""
	@echo
	@echo "Other MCP clients: URL $(HTTP_URL) with that Authorization header. Revoke with make lan-revoke NAME=$(NAME)."

lan-revoke:
	@test -f "$(LAN_TOKENS)/$(NAME)-token" || { echo 'Usage: make lan-revoke NAME=computer-name (no such token)'; exit 2; }
	@"$(VENV)/bin/research-http-token" revoke --registry "$(TOKEN_REGISTRY)" --token-file "$(LAN_TOKENS)/$(NAME)-token"
	@rm -f "$(LAN_TOKENS)/$(NAME)-token"

SHARE_PORT ?= 8002
SHARE_DAYS ?= 14

share:
	@test -x "$(VENV)/bin/cloudflared" || command -v cloudflared >/dev/null || "$(MAKE)" -s cloudflared
	@"$(PY)" -m research_library.share --token-registry "$(TOKEN_REGISTRY)" --port "$(SHARE_PORT)" --days "$(SHARE_DAYS)" --profile "$(PROFILE)" -- $(if $(COLLECTION_REGISTRY),$(COLLECTION_ARGS),--release "$(RELEASE)")

cloudflared:
	@url=https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/'); \
	echo "Downloading $$url"; \
	curl -fsSL -o "$(VENV)/bin/cloudflared.tmp" "$$url" && chmod 755 "$(VENV)/bin/cloudflared.tmp" && mv "$(VENV)/bin/cloudflared.tmp" "$(VENV)/bin/cloudflared" && "$(VENV)/bin/cloudflared" --version

connect connect-claude:
	@"$(PY)" -m research_library.local_client connect $(CLIENT_ARGS)

disconnect:
	@"$(PY)" -m research_library.local_client disconnect $(CLIENT_ARGS)

reconnect:
	@"$(PY)" -m research_library.local_client reconnect $(CLIENT_ARGS)

test-http:
	"$(PY)" tests/check_http.py

test-connection:
	"$(PY)" tests/check_connection.py

test-collections:
	"$(PY)" tests/check_collections.py

test-catalog:
	"$(PY)" tests/check_catalog.py

test-citations:
	"$(PY)" -m unittest discover -s tests -p 'test_citation_neighborhood.py' -v

test-project:
	"$(PY)" -m unittest discover -s tests -p 'test_project*.py' -v
	"$(PY)" -m unittest discover -s tests -p 'test_citation_neighborhood.py' -v

container-build:
	$(CONTAINER_ENGINE) build $(if $(filter podman,$(notdir $(CONTAINER_ENGINE))),--format docker,) -t "$(IMAGE)" .

test-container:
	"$(PY)" tests/check_container.py --engine "$(CONTAINER_ENGINE)" --image "$(IMAGE)"

evaluate:
	"$(PY)" tests/check_evaluation.py

test-install:
	"$(PY)" tests/check_install.py

check: test evaluate test-http test-collections test-catalog test-install
