# minutaur: `make install` for daily use, `make test` for development.

PYTHON   ?= python3
APP_DIR  ?= $(HOME)/.local/share/minutaur
BIN_DIR  ?= $(HOME)/.local/bin
UNIT_DIR ?= $(HOME)/.config/systemd/user
CONF_DIR ?= $(or $(XDG_CONFIG_HOME),$(HOME)/.config)/minutaur
DEV_VENV := .venv

.DEFAULT_GOAL := help
.PHONY: help check-deps install enable disable uninstall test clean

help: ## show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-11s %s\n", $$1, $$2}'

check-deps: ## check the Ubuntu packages minutaur needs
	@missing=""; \
	command -v parec >/dev/null || missing="$$missing pulseaudio-utils"; \
	command -v ffmpeg >/dev/null || missing="$$missing ffmpeg"; \
	command -v notify-send >/dev/null || missing="$$missing libnotify-bin"; \
	$(PYTHON) -c 'import ensurepip' 2>/dev/null || missing="$$missing python3-venv"; \
	if [ -n "$$missing" ]; then echo "Missing packages. Run:  sudo apt install$$missing"; exit 1; fi
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10+ required"; exit 1; }

install: check-deps ## install (or upgrade) minutaur for this user, no sudo
	$(PYTHON) -m venv $(APP_DIR)/venv
	$(APP_DIR)/venv/bin/pip install --quiet --upgrade pip
	$(APP_DIR)/venv/bin/pip install --quiet --upgrade .
	mkdir -p $(BIN_DIR) $(UNIT_DIR) $(CONF_DIR)/skills
	ln -sf $(APP_DIR)/venv/bin/minutaur $(BIN_DIR)/minutaur
	install -m 644 contrib/minutaur.service $(UNIT_DIR)/minutaur.service
	@systemctl --user daemon-reload 2>/dev/null || true
	@test -e $(CONF_DIR)/env || (umask 177; printf '%s\n' \
	    '# minutaur environment, one KEY=VALUE per line. Notes use Claude in Amazon Bedrock' \
	    '# with your normal AWS credentials; set these only if you need to.' \
	    '# AWS_REGION=us-west-2' '# AWS_PROFILE=' '# AWS_BEARER_TOKEN_BEDROCK=' \
	    '# ANTHROPIC_API_KEY=    (only with provider = "anthropic")' > $(CONF_DIR)/env)
	@echo
	@echo "Installed $(BIN_DIR)/minutaur. Next:"
	@echo "  minutaur doctor   check audio, AWS credentials and skills"
	@echo "  minutaur record   record a meeting now (Ctrl-C to stop)"
	@echo "  make enable       record every browser call automatically"
	@case ":$$PATH:" in *":$(BIN_DIR):"*) ;; *) echo "Note: $(BIN_DIR) is not on your PATH.";; esac

enable: ## start the background recorder now and at every login
	systemctl --user enable --now minutaur

disable: ## stop the background recorder
	@systemctl --user disable --now minutaur 2>/dev/null || true

uninstall: disable ## remove minutaur (keeps your config, skills and meetings)
	rm -f $(BIN_DIR)/minutaur $(UNIT_DIR)/minutaur.service
	rm -rf $(APP_DIR)
	@systemctl --user daemon-reload 2>/dev/null || true

$(DEV_VENV)/.installed: pyproject.toml
	$(PYTHON) -m venv $(DEV_VENV)
	$(DEV_VENV)/bin/pip install --quiet --upgrade pip
	$(DEV_VENV)/bin/pip install --quiet -e '.[dev]'
	touch $@

test: $(DEV_VENV)/.installed ## run the test suite (no audio hardware or credentials needed)
	$(DEV_VENV)/bin/pytest -q $(ARGS)

clean: ## remove the dev environment and build leftovers
	rm -rf $(DEV_VENV) build dist .pytest_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
