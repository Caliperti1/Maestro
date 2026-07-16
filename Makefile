SHELL := /bin/bash

EVEN_DIR := EvenG2/maestro-even-client

TAILSCALE_IP ?= 100.66.109.2
RUNTIME_DIR ?= $(HOME)/Maestro-runtime

.PHONY: help even-install even-dev even-sim even-sim-auto even-up even-build backend-reload frontend-tailscale runtime-setup runtime-backend-reload runtime-frontend-tailscale

help:
	@echo "Available targets:"
	@echo "  make even-install   - Install EvenG2 client dependencies"
	@echo "  make even-dev       - Start EvenG2 dev server on port 5174"
	@echo "  make even-sim       - Start Even Hub simulator against localhost:5174"
	@echo "  make even-sim-auto  - Start simulator with automation API on :9898"
	@echo "  make even-up        - Start dev server + simulator together"
	@echo "  make even-build     - Build EvenG2 client"
	@echo "  make backend-reload - Start Maestro backend with source autoreload"
	@echo "  make frontend-tailscale - Start Maestro frontend for this Tailnet"
	@echo "  make runtime-setup - Create/refresh the dedicated main runtime worktree"
	@echo "  make runtime-backend-reload - Start backend from the dedicated runtime"
	@echo "  make runtime-frontend-tailscale - Start frontend from the dedicated runtime"

even-install:
	cd $(EVEN_DIR) && npm install

even-dev:
	cd $(EVEN_DIR) && npm run dev:host

even-sim:
	cd $(EVEN_DIR) && npm run sim

even-sim-auto:
	cd $(EVEN_DIR) && npm run sim:auto

even-up:
	cd $(EVEN_DIR) && bash -lc 'npm run dev:host & DEV_PID=$$!; trap "kill $$DEV_PID" EXIT INT TERM; npm run sim'

even-build:
	cd $(EVEN_DIR) && npm run build

backend-reload:
	./.venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --reload

frontend-tailscale:
	cd frontend && VITE_API_BASE_URL=http://$(TAILSCALE_IP):8000 npm run dev -- --host 0.0.0.0

runtime-setup:
	@if [ ! -d "$(RUNTIME_DIR)" ]; then git worktree add "$(RUNTIME_DIR)" main; fi
	@git -C "$(RUNTIME_DIR)" config core.excludesFile "$(HOME)/.maestro-runtime-gitignore"
	@printf ".venv\nfrontend/node_modules\n" > "$(HOME)/.maestro-runtime-gitignore"
	@if [ ! -e "$(RUNTIME_DIR)/.env" ] && [ -e "$(CURDIR)/.env" ]; then ln -s "$(CURDIR)/.env" "$(RUNTIME_DIR)/.env"; fi
	@if [ ! -e "$(RUNTIME_DIR)/.venv" ] && [ -d "$(CURDIR)/.venv" ]; then ln -s "$(CURDIR)/.venv" "$(RUNTIME_DIR)/.venv"; fi
	@if [ ! -e "$(RUNTIME_DIR)/frontend/node_modules" ] && [ -d "$(CURDIR)/frontend/node_modules" ]; then ln -s "$(CURDIR)/frontend/node_modules" "$(RUNTIME_DIR)/frontend/node_modules"; fi
	@echo "Dedicated runtime ready at $(RUNTIME_DIR)"

runtime-backend-reload:
	cd "$(RUNTIME_DIR)" && ./.venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --reload

runtime-frontend-tailscale:
	cd "$(RUNTIME_DIR)/frontend" && VITE_API_BASE_URL=http://$(TAILSCALE_IP):8000 npm run dev -- --host 0.0.0.0
