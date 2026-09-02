# hermes-agent Makefile
# Primary target: deploy the VCG dispatcher and companion scripts to the
# runtime location so the live cluster continues to work after a git pull.
#
# Usage:
#   make deploy-dispatch   — copy scripts/dispatch/* → ~/.hermes/scripts/
#   make verify-dispatch   — diff in-repo vs deployed; exit 1 if any drift
#   make test-dispatch     — run unit tests against the in-repo copy
#   make all               — deploy-dispatch + test-dispatch

DISPATCH_SRC := scripts/dispatch
# Use the real ubuntu home, not the profile-redirected $HOME.
# On Hermes workers $HOME is remapped to the profile sandbox, but the
# runtime dispatcher lives at the actual ubuntu home path.
DISPATCH_DEST := /home/ubuntu/.hermes/scripts

# Files tracked under scripts/dispatch/ that must be deployed to runtime.
# hrv_node_state_reader.py is a sibling dependency imported by name at runtime.
DISPATCH_FILES := llm_cluster_dispatcher.py hrv_node_state_reader.py

.PHONY: all deploy-dispatch verify-dispatch test-dispatch

all: deploy-dispatch test-dispatch

## deploy-dispatch: copy tracked dispatcher + companions to ~/.hermes/scripts/
deploy-dispatch:
	@echo "[deploy-dispatch] Syncing $(DISPATCH_SRC)/ → $(DISPATCH_DEST)/"
	@mkdir -p $(DISPATCH_DEST)
	@for f in $(DISPATCH_FILES); do \
		src="$(DISPATCH_SRC)/$$f"; \
		dst="$(DISPATCH_DEST)/$$f"; \
		if [ ! -f "$$src" ]; then \
			echo "  MISSING $$src — skipping"; \
			continue; \
		fi; \
		if cmp -s "$$src" "$$dst"; then \
			echo "  UNCHANGED $$f"; \
		else \
			cp "$$src" "$$dst"; \
			echo "  DEPLOYED  $$f"; \
		fi; \
	done
	@echo "[deploy-dispatch] Done."

## verify-dispatch: exit 1 if deployed copies have drifted from the repo
verify-dispatch:
	@echo "[verify-dispatch] Checking for drift between repo and deployed copies..."
	@drift=0; \
	for f in $(DISPATCH_FILES); do \
		src="$(DISPATCH_SRC)/$$f"; \
		dst="$(DISPATCH_DEST)/$$f"; \
		if [ ! -f "$$dst" ]; then \
			echo "  NOT DEPLOYED: $$f (run: make deploy-dispatch)"; \
			drift=1; \
		elif ! cmp -s "$$src" "$$dst"; then \
			echo "  DRIFT DETECTED: $$f"; \
			diff -u "$$src" "$$dst" | head -30; \
			drift=1; \
		else \
			echo "  OK $$f"; \
		fi; \
	done; \
	exit $$drift

## test-dispatch: run unit tests against the in-repo scripts/dispatch/ copy
test-dispatch:
	@echo "[test-dispatch] Running unit tests..."
	python -m pytest tests/test_llm_cluster_dispatcher_unit.py -v --tb=short
