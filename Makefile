.PHONY: install
install: ## Install the virtual environment and install the pre-commit hooks
	@echo "🚀 Creating virtual environment using uv"
	@uv sync
	@uv run pre-commit install

.PHONY: check
check: ## Run code quality tools.
	@echo "🚀 Checking lock file consistency with 'pyproject.toml'"
	@uv lock --locked
	@echo "🚀 Linting code: Running pre-commit"
	@uv run pre-commit run -a
	@echo "🚀 Static type checking: Running ty"
	@uv run ty check

.PHONY: release-dry
release-dry: ## Simulate the release process and show the next version
	@echo "🚀 Simulating release process..."
	@uv run semantic-release --noop version --print

.PHONY: release-stable
release-stable: ## Prepare project for stable 1.0.0 release (disables 0.x.x versions)
	@echo "🚀 Preparing for stable release..."
	@sed -i 's/allow_zero_version = true/allow_zero_version = false/' pyproject.toml
	@echo "Updated pyproject.toml: allow_zero_version = false"
	@echo "Next steps:"
	@echo "   1. git add pyproject.toml"
	@echo "   2. git commit -m 'chore: prepare for stable 1.0.0 release'"
	@echo "   3. Push to main - the next feat/fix commit will trigger 1.0.0"

.PHONY: docs-check
docs-check: ## Show TUI/MCP surface changes since their docs page was last updated
	@last=$$(git log -1 --format=%H -- site-docs/docs/keybindings.md); \
	echo "📚 keybindings.md last touched at $$last"; \
	git diff --stat $$last..HEAD -- \
	    odoo_activity/tui.py \
	    odoo_activity/panes/detail.py
	@last=$$(git log -1 --format=%H -- site-docs/docs/mcp.md); \
	echo "📚 mcp.md last touched at $$last"; \
	git diff --stat $$last..HEAD -- \
	    odoo_activity/mcp_server.py

.PHONY: test
test: ## Test the code with pytest
	@echo "🚀 Testing code: Running pytest"
	@uv run python -m pytest --doctest-modules


.PHONY: build
build: clean-build ## Build wheel file
	@echo "🚀 Creating wheel file"
	@uvx --from build pyproject-build --installer uv

.PHONY: clean-build
clean-build: ## Clean build artifacts
	@echo "🚀 Removing build artifacts"
	@uv run python -c "import shutil; import os; shutil.rmtree('dist') if os.path.exists('dist') else None"

.PHONY: run
run: ## Run the cli
	@uv run odoo-activity

.PHONY: help
help:
	@uv run python -c "import re; \
	[[print(f'\033[36m{m[0]:<20}\033[0m {m[1]}') for m in re.findall(r'^([a-zA-Z_-]+):.*?## (.*)$$', open(makefile).read(), re.M)] for makefile in ('$(MAKEFILE_LIST)').strip().split()]"

.DEFAULT_GOAL := help

.PHONY: docs
docs: ## Build the documentation site
	@echo "🚀 Building documentation site"
	@uv run zensical build

.PHONY: docs-serve
docs-serve: ## Serve the documentation site locally
	@echo "🚀 Serving documentation site"
	@uv run zensical serve

.PHONY: demo
demo: ## Render site-docs/demo.tape → site-docs/docs/demo.gif via Docker VHS
	@echo "🚀 Installing odoo-activity into a throwaway venv for the demo"
	@rm -rf .demo-venv && mkdir -p .demo-venv
	@docker run --rm --entrypoint sh --user "$$(id -u):$$(id -g)" -e HOME=/tmp \
		-v $(PWD):/src:ro \
		-v $(PWD)/.demo-venv:/venv \
		ghcr.io/charmbracelet/vhs:latest -c '\
			python3 -c "import urllib.request as u; u.urlretrieve(\"https://bootstrap.pypa.io/get-pip.py\",\"/tmp/g.py\")" && \
			python3 /tmp/g.py -q --break-system-packages --root-user-action=ignore --user && \
			export PATH=$$HOME/.local/bin:$$PATH && \
			pip install -q --break-system-packages --root-user-action=ignore --prefix=/venv /src'
	@echo "🚀 Rendering demo GIF"
	@docker run --rm \
		-v $(PWD)/site-docs:/vhs \
		-v $(PWD)/.demo-venv:/venv:ro \
		ghcr.io/charmbracelet/vhs:latest demo.tape
	@rm -rf .demo-venv
