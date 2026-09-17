.ONESHELL:
.SHELLFLAGS := -ec
ENV_PREFIX=$(shell python -c "if __import__('pathlib').Path('.venv/bin/pip').exists(): print('.venv/bin/')")

.PHONY: help
help:             ## Show the help.
	@echo "Usage: make <target>"
	@echo ""
	@echo "Targets:"
	@fgrep "##" Makefile | fgrep -v fgrep

.PHONY: fmt
fmt:              ## Format code using ruff.
	$(ENV_PREFIX)python -m ruff format src tests examples

.PHONY: lint
lint:             ## Check format, lint, and types.
	$(ENV_PREFIX)python -m ruff format --check src tests examples
	$(ENV_PREFIX)python -m ruff check src tests examples
	$(ENV_PREFIX)python -m ty check src/copper_pilot_cli

.PHONY: test
test:             ## Run tests excluding live hosted checks.
	$(ENV_PREFIX)python -m pytest -m "not live"

.PHONY: release
release:          ## Create a new tag for release.
	@set -e; \
	echo "WARNING: This operation will create a version tag and push to github"; \
	printf "Version? (provide the next x.y.z semver): "; \
	read -r TAG; \
	test -n "$$TAG" || { echo "Version is required" >&2; exit 1; }; \
	case "$$TAG" in v*) echo "Version must be X.Y.Z without a v prefix" >&2; exit 1 ;; esac; \
	echo "$$TAG" | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$$' \
		|| { echo "Version must be X.Y.Z" >&2; exit 1; }; \
	echo "$$TAG" > VERSION; \
	if [ -x "$(ENV_PREFIX)gitchangelog" ]; then \
		$(ENV_PREFIX)gitchangelog > HISTORY.md; \
	elif [ -x "$(ENV_PREFIX)python" ]; then \
		$(ENV_PREFIX)python -m gitchangelog.gitchangelog > HISTORY.md; \
	else \
		python3 -m gitchangelog.gitchangelog > HISTORY.md; \
	fi; \
	git add VERSION HISTORY.md; \
	git commit -m "release: version $$TAG 🚀"; \
	echo "creating git tag : $$TAG"; \
	git tag "$$TAG"; \
	git push -u origin HEAD --tags; \
	echo "Github Actions will detect the new tag and release the new version."
