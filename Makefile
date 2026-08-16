# Lindos — top-level Makefile (SPEC §1)
#
#   make            → help
#   make debs       → build every packages/<name> into out/debs/
#   make kernel     → build the Lindos-tuned kernel .debs (Linux host only;
#                     runs build/kernel/build-kernel.sh → out/kernel/*.deb).
#                     KERNEL_ARGS="--version 6.14 --jobs 8" passes flags through.
#   make assets     → build/fetch-assets.sh --out out/assets
#   make iso        → full ISO build: build-iso.sh runs mkdeb.sh (debs), then
#                     fetch-assets.sh (assets), then the chroot + repack.
#                     Needs root (sudo make iso, or make iso → sudo -E)
#   make iso-docker → same, inside the ubuntu:24.04 builder container
#   make qemu       → boot the newest out/lindos-*.iso (BIOS)
#   make qemu-uefi  → boot it with OVMF (UEFI)
#   make test       → tests/run.sh (lint + pytest + JSON/XML/.desktop checks)
#   make lint       → shell/python syntax + shellcheck (if installed)
#   make clean      → remove build products (keeps the ISO cache + assets)
#   make distclean  → remove out/ entirely
#
# Variables you may override on the command line or in the environment:
#   INCLUDE_WINE=1 INCLUDE_STEAM=1 INCLUDE_FLATPAK_LAUNCHERS=0 KISAK_MESA=0
#   BASE_ISO=path/to/linuxmint-22.2-xfce-64bit.iso   (local base ISO)
#   ISO_ARGS="--skip-download --no-cleanup"          (extra build-iso.sh flags)
#   PYTHON=python3  SUDO=sudo
SHELL := /bin/bash
.SHELLFLAGS := -Eeuo pipefail -c
.DEFAULT_GOAL := help
.ONESHELL:

PYTHON  ?= python3
# no sudo when already root (sudo make iso, Docker, CI runners)
SUDO    ?= $(shell [ "$$(id -u)" -eq 0 ] && echo "" || echo sudo)
ISO_ARGS ?=
BASE_ISO ?=
OUT_DIR ?= out
BUILD_ISO_FLAGS := $(ISO_ARGS)
ifneq ($(strip $(BASE_ISO)),)
BUILD_ISO_FLAGS += --iso "$(BASE_ISO)"
endif

# every bash script + python file we lint
SHELL_SCRIPTS := $(shell find build packages tests -type f \( -name '*.sh' -o -path '*/DEBIAN/postinst' -o -path '*/DEBIAN/prerm' -o -path '*/DEBIAN/postrm' -o -path '*/DEBIAN/preinst' \) 2>/dev/null | sort)
PY_FILES      := $(shell find build packages tests -type f -name '*.py' 2>/dev/null | sort)

KERNEL_ARGS ?=

.PHONY: help debs kernel assets iso iso-docker docker-iso qemu qemu-uefi test lint clean distclean

help: ## show this help
	@printf 'Lindos build targets:\n'
	grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	printf '\nOverrides: INCLUDE_WINE INCLUDE_STEAM INCLUDE_FLATPAK_LAUNCHERS KISAK_MESA BASE_ISO ISO_ARGS (see build/config.env)\n'

debs: ## build all .deb packages into out/debs/
	@bash build/mkdeb.sh all

kernel: ## build the Lindos-tuned kernel .debs (Linux host only → out/kernel/)
	@if [ "$$(uname -s)" != "Linux" ]; then
		echo "make kernel: the kernel can only be compiled on a Linux host (got $$(uname -s))." >&2
		echo "  Run this on Ubuntu 24.04 / Debian, or inside the ubuntu:24.04 builder (make iso-docker)." >&2
		exit 2
	fi
	if [ -x build/kernel/build-kernel.sh ] || [ -f build/kernel/build-kernel.sh ]; then
		bash build/kernel/build-kernel.sh $(KERNEL_ARGS)
	else
		echo "make kernel: build/kernel/build-kernel.sh not found." >&2
		echo "  The kernel build recipe (SPEC-KERNEL §15.5) is part of the lindos-kernel component;" >&2
		echo "  check it out, then re-run 'make kernel'. The ISO build works without it (stock kernel)." >&2
		exit 2
	fi

assets: ## fetch/build theme, icon, cursor and font assets into out/assets/
	@ASSETS_DIR="$(OUT_DIR)/assets" bash build/fetch-assets.sh --out "$(OUT_DIR)/assets"

iso: ## build the ISO (debs → assets → chroot hooks → repack; needs root, uses sudo -E)
	@$(SUDO) $(if $(SUDO),-E,) bash build/build-iso.sh $(BUILD_ISO_FLAGS)

iso-docker: ## build the ISO inside Docker (privileged ubuntu:24.04 container)
	@bash build/docker-build.sh -- $(BUILD_ISO_FLAGS)

docker-iso: iso-docker ## alias of iso-docker

qemu: ## boot the newest out/lindos-*.iso in QEMU (BIOS)
	@bash build/test-qemu.sh $(QEMU_ARGS)

qemu-uefi: ## boot it in QEMU with OVMF (UEFI)
	@bash build/test-qemu.sh --uefi $(QEMU_ARGS)

test: ## run tests/run.sh (lint + pytest + data checks)
	@if [ -x tests/run.sh ] || [ -f tests/run.sh ]; then
		bash tests/run.sh
	else
		echo "tests/run.sh not present — running pytest directly"
		$(PYTHON) -m pytest -q -c tests/pytest.ini --rootdir . tests packages/*/tests build/tests
	fi

lint: ## syntax-check every shell/python file, shellcheck if available
	@rc=0
	for f in $(SHELL_SCRIPTS); do
		first="$$(head -n1 "$$f")"
		case "$$first" in
			'#!/bin/bash'*|'#!/usr/bin/env bash'*) bash -n "$$f" || { echo "bash -n FAILED: $$f"; rc=1; } ;;
			'#!/bin/sh'*) sh -n "$$f" 2>/dev/null || bash --posix -n "$$f" || { echo "sh -n FAILED: $$f"; rc=1; } ;;
			*) bash -n "$$f" || { echo "bash -n FAILED: $$f"; rc=1; } ;;
		esac
	done
	for f in $(PY_FILES); do
		$(PYTHON) -m py_compile "$$f" || { echo "py_compile FAILED: $$f"; rc=1; }
	done
	if command -v shellcheck >/dev/null 2>&1; then
		shellcheck -x -S warning $(SHELL_SCRIPTS) || rc=1
	else
		echo "shellcheck not installed — skipped"
	fi
	if [ -f build/config.env ]; then bash -n build/config.env || rc=1; fi
	for j in $$(find build packages -name '*.json' 2>/dev/null); do
		$(PYTHON) -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$$j" || { echo "invalid JSON: $$j"; rc=1; }
	done
	exit $$rc

clean: ## remove build products (keeps out/cache and out/assets)
	@rm -rf $(OUT_DIR)/work $(OUT_DIR)/debs $(OUT_DIR)/hooks $(OUT_DIR)/qemu $(OUT_DIR)/*.iso $(OUT_DIR)/*.sha256 $(OUT_DIR)/build.log
	find . -type d \( -name '__pycache__' -o -name '.pytest_cache' \) -prune -exec rm -rf {} + 2>/dev/null || true

distclean: clean ## remove out/ entirely (ISO cache + assets too)
	@rm -rf $(OUT_DIR)
