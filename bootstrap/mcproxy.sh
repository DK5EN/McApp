#!/bin/bash
# mcproxy.sh - Unified Installer/Updater for MCProxy
# Idempotent, self-healing, Trixie-compatible
# One script for: bootstrap, configure, upgrade, repair
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/bootstrap/mcproxy.sh | bash
#   ./mcproxy.sh [OPTIONS]
#
# Options:
#   --check       Dry-run: show what would be updated
#   --force       Skip version checks, reinstall everything
#   --reconfigure Re-prompt for configuration values
#   --fix         Repair mode: reinstall broken components
#   --quiet       Minimal output (for cron jobs)
#   --version     Show script version and exit

set -euo pipefail

#──────────────────────────────────────────────────────────────────
# CONSTANTS
#──────────────────────────────────────────────────────────────────
readonly SCRIPT_VERSION="2.0.0"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly GITHUB_RAW_BASE="https://raw.githubusercontent.com/DK5EN/McAdvChat/main"

# Installation paths
readonly CONFIG_DIR="/etc/mcadvchat"
readonly CONFIG_FILE="${CONFIG_DIR}/config.json"
readonly WEBAPP_DIR="/var/www/html/webapp"
readonly SCRIPTS_DIR="/usr/local/bin"
readonly SHARE_DIR="/usr/local/share/mcproxy"
readonly GITHUB_REPO="DK5EN/McAdvChat"
readonly GITHUB_API_BASE="https://api.github.com/repos/${GITHUB_REPO}"

# User home directory (handles sudo correctly)
# When running with sudo, $HOME is /root, but we want the actual user's home
get_real_home() {
  if [[ -n "$SUDO_USER" ]]; then
    getent passwd "$SUDO_USER" | cut -d: -f6
  else
    echo "$HOME"
  fi
}

# Paths set after we know the real home
VENV_DIR=""
OLD_VENV_DIR=""
INSTALL_DIR=""

init_paths() {
  local real_home
  real_home=$(get_real_home)
  INSTALL_DIR="${real_home}/mcproxy"
  VENV_DIR="${real_home}/mcproxy-venv"
  OLD_VENV_DIR="${real_home}/venv"
}

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m' # No Color

#──────────────────────────────────────────────────────────────────
# GLOBAL FLAGS (set by parse_args)
#──────────────────────────────────────────────────────────────────
DRY_RUN=false
FORCE=false
RECONFIGURE=false
FIX_MODE=false
QUIET=false

#──────────────────────────────────────────────────────────────────
# LOGGING
#──────────────────────────────────────────────────────────────────
log_info() {
  [[ "$QUIET" == "true" ]] && return
  echo -e "${BLUE}[INFO]${NC} $*"
}

log_ok() {
  [[ "$QUIET" == "true" ]] && return
  echo -e "${GREEN}[OK]${NC} $*"
}

log_warn() {
  echo -e "${YELLOW}[WARN]${NC} $*" >&2
}

log_error() {
  echo -e "${RED}[ERROR]${NC} $*" >&2
}

log_step() {
  [[ "$QUIET" == "true" ]] && return
  echo -e "${GREEN}==>${NC} $*"
}

#──────────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
#──────────────────────────────────────────────────────────────────
require_root() {
  if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (use sudo)"
    exit 1
  fi
}

command_exists() {
  command -v "$1" &>/dev/null
}

#──────────────────────────────────────────────────────────────────
# SOURCE LIBRARY FILES
#──────────────────────────────────────────────────────────────────
source_libs() {
  local lib_dir

  # If running from bootstrap directory, use local libs
  if [[ -d "${SCRIPT_DIR}/lib" ]]; then
    lib_dir="${SCRIPT_DIR}/lib"
  # If installed to share dir, use those
  elif [[ -d "${SHARE_DIR}/lib" ]]; then
    lib_dir="${SHARE_DIR}/lib"
  else
    log_error "Cannot find library files"
    exit 1
  fi

  # shellcheck source=lib/detect.sh
  source "${lib_dir}/detect.sh"
  # shellcheck source=lib/config.sh
  source "${lib_dir}/config.sh"
  # shellcheck source=lib/system.sh
  source "${lib_dir}/system.sh"
  # shellcheck source=lib/packages.sh
  source "${lib_dir}/packages.sh"
  # shellcheck source=lib/deploy.sh
  source "${lib_dir}/deploy.sh"
  # shellcheck source=lib/health.sh
  source "${lib_dir}/health.sh"
}

#──────────────────────────────────────────────────────────────────
# CLI PARSING
#──────────────────────────────────────────────────────────────────
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check)
        DRY_RUN=true
        shift
        ;;
      --force)
        FORCE=true
        shift
        ;;
      --reconfigure)
        RECONFIGURE=true
        shift
        ;;
      --fix)
        FIX_MODE=true
        shift
        ;;
      --quiet)
        QUIET=true
        shift
        ;;
      --version)
        echo "mcproxy.sh version ${SCRIPT_VERSION}"
        exit 0
        ;;
      --help|-h)
        show_help
        exit 0
        ;;
      *)
        log_error "Unknown option: $1"
        show_help
        exit 1
        ;;
    esac
  done
}

show_help() {
  cat << EOF
mcproxy.sh - Unified Installer/Updater for MCProxy

Usage:
  ./mcproxy.sh [OPTIONS]

Options:
  --check       Dry-run: show what would be updated
  --force       Skip version checks, reinstall everything
  --reconfigure Re-prompt for configuration values
  --fix         Repair mode: reinstall broken components
  --quiet       Minimal output (for cron jobs)
  --version     Show script version and exit
  --help, -h    Show this help message

Examples:
  # Fresh install or update
  curl -fsSL ${GITHUB_RAW_BASE}/bootstrap/mcproxy.sh | sudo bash

  # Check what would be updated
  sudo ./mcproxy.sh --check

  # Force reinstall everything
  sudo ./mcproxy.sh --force

  # Repair broken installation
  sudo ./mcproxy.sh --fix

  # Change configuration
  sudo ./mcproxy.sh --reconfigure
EOF
}

#──────────────────────────────────────────────────────────────────
# MAIN
#──────────────────────────────────────────────────────────────────
main() {
  parse_args "$@"

  # Show banner
  if [[ "$QUIET" != "true" ]]; then
    echo ""
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║           MCProxy Bootstrap v${SCRIPT_VERSION}                      ║"
    echo "║   MeshCom Message Proxy for Raspberry Pi                 ║"
    echo "╚══════════════════════════════════════════════════════════╝"
    echo ""
  fi

  require_root

  # Initialize paths that depend on the real user's home
  init_paths

  source_libs

  # Phase 1: Detect current state
  log_step "Detecting system state..."
  local state
  state=$(detect_install_state)
  local debian_codename
  debian_codename=$(get_debian_codename)
  local python_version
  python_version=$(get_python_version)

  log_info "Debian: ${debian_codename}"
  log_info "Python: ${python_version}"
  log_info "Install state: ${state}"

  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "Dry-run mode - no changes will be made"
    dry_run_report "$state"
    exit 0
  fi

  # Phase 1.5: Migration from old installation (if needed)
  if [[ "$state" == "migrate" ]]; then
    log_step "Detected old installation - migrating..."
    migrate_old_installation
    # After migration prep, treat as upgrade
    state="upgrade"
  fi

  # Phase 2: Configuration (only if needed)
  if [[ "$state" == "fresh" || "$state" == "incomplete" || "$RECONFIGURE" == "true" ]]; then
    log_step "Collecting configuration..."
    collect_config "$state"
  else
    log_info "Using existing configuration"
  fi

  # Phase 3: System setup
  log_step "Configuring system..."
  setup_system

  # Phase 4: Package installation
  log_step "Installing packages..."
  install_packages

  # Phase 5: Application deployment
  log_step "Deploying application..."
  deploy_app "$FORCE"

  # Phase 6: Service activation
  log_step "Activating services..."
  activate_services

  # Phase 7: Health check
  log_step "Running health checks..."
  if health_check; then
    print_success_summary
  else
    log_error "Health checks failed - check logs above"
    exit 1
  fi
}

#──────────────────────────────────────────────────────────────────
# DRY RUN REPORT
#──────────────────────────────────────────────────────────────────
dry_run_report() {
  local state="$1"

  echo ""
  echo "═══════════════════════════════════════════════════════════"
  echo "  DRY RUN REPORT"
  echo "═══════════════════════════════════════════════════════════"
  echo ""

  echo "Current State: ${state}"
  echo ""

  echo "Would perform the following actions:"
  echo ""

  case "$state" in
    fresh)
      echo "  [CONFIG] Prompt for configuration values"
      echo "  [SYSTEM] Configure tmpfs for /var/log and /tmp"
      echo "  [SYSTEM] Configure nftables firewall"
      echo "  [SYSTEM] Configure journald for volatile storage"
      echo "  [SYSTEM] Disable unused services"
      echo "  [PACKAGES] Install uv package manager"
      echo "  [PACKAGES] Install apt packages (jq, curl, screen, etc.)"
      echo "  [PACKAGES] Install and configure Caddy"
      echo "  [PACKAGES] Install and configure lighttpd"
      echo "  [DEPLOY] Download release tarball to ~/mcproxy"
      echo "  [DEPLOY] Run uv sync to install Python dependencies"
      echo "  [DEPLOY] Download and install webapp"
      echo "  [SERVICES] Enable and start mcproxy, caddy, lighttpd"
      ;;
    incomplete)
      echo "  [CONFIG] Resume configuration prompts"
      echo "  [PACKAGES] Verify/update packages"
      echo "  [DEPLOY] Verify/update application"
      echo "  [SERVICES] Enable and start services"
      ;;
    migrate)
      echo "  [MIGRATE] Detected old installation (/usr/local/bin scripts)"
      echo "  [MIGRATE] Stop mcproxy service"
      echo "  [MIGRATE] Download release tarball to ~/mcproxy"
      echo "  [MIGRATE] Run uv sync for dependencies"
      echo "  [MIGRATE] Update systemd service to use 'uv run mcproxy'"
      echo "  [MIGRATE] Add missing config fields"
      echo "  [SYSTEM] Configure tmpfs, firewall, journald"
      echo "  [PACKAGES] Install uv, update dependencies"
      echo "  [SERVICES] Restart with new configuration"
      echo ""
      echo "  Note: Your existing config.json will be preserved."
      echo "  Old files in /usr/local/bin will NOT be deleted."
      ;;
    upgrade)
      check_versions_report
      ;;
  esac

  echo ""
}

# Run main
main "$@"
