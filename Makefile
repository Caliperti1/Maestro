SHELL := /bin/bash

EVEN_DIR := EvenG2/maestro-even-client

.PHONY: help even-install even-dev even-sim even-sim-auto even-up even-build

help:
	@echo "Available targets:"
	@echo "  make even-install   - Install EvenG2 client dependencies"
	@echo "  make even-dev       - Start EvenG2 dev server on port 5174"
	@echo "  make even-sim       - Start Even Hub simulator against localhost:5174"
	@echo "  make even-sim-auto  - Start simulator with automation API on :9898"
	@echo "  make even-up        - Start dev server + simulator together"
	@echo "  make even-build     - Build EvenG2 client"

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
