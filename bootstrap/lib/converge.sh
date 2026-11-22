#!/bin/bash
# converge.sh - Step-gated system convergence for `mcapp.sh --converge`
#
# Each step carries the epoch it LAST CHANGED in. A step runs only when the
# box's installed epoch is below that value, so an epoch bump that touched one
# step no longer re-runs the firewall and a full apt upgrade.
#
# Rules:
#   * When you change a step's behaviour, raise its epoch below to the new
#     SYSTEM_EPOCH (bootstrap/mcapp.sh) in the same change.
#   * Every function called by setup_system() (system.sh) and by
#     install_packages() (packages.sh) must have a row here, in the same order
#     (pinned by scripts/bootstrap_converge_steps_tests.py). The full run and
#     --skip still use setup_system/install_packages and are unaffected.
#   * ensure_web_frontend is NOT gated: it is a no-op when current and it
#     applies template version bumps (e.g. the Caddyfile).
#
# set -e semantics (decision): setup steps are called as plain commands, so a
# failing step aborts the whole run (the caller must also call
# converge_system_steps as a plain command, never inside if/||/!). The package
# group (install_apt_deps, install_uv, ensure_web_frontend) is detected by
# return status; the function logs the "epoch NOT advanced" error itself and
# returns 1. Either way the caller never reaches write_system_epoch.
#
# Stricter than the old `if ! install_packages` on purpose. That one tested only
# the LAST function, so a failed install_apt_deps (apt, or the link check in
# verify_link_state) was ignored and the epoch advanced. Here each package step
# is tested, so the run stops and the watchdog retries. The full run still has
# the old masking.

# "<function> <last-changed-epoch>", in execution order.
converge_step_table() {
  cat <<'TABLE'
disable_unused_services 8
remove_bloat_packages 8
configure_tmpfs 8
configure_logdirs 9
configure_boot_memory 8
configure_journald 8
configure_logrotate 8
configure_firewall 9
configure_bluetooth 8
disable_ipv6 8
configure_unattended_upgrades 8
configure_wpasupplicant_pin 8
configure_fast_ssh_login 8
configure_ssh_hardening 10
install_apt_deps 8
install_uv 8
TABLE
}

# Steps whose failure is detected by return status (package phase).
_converge_is_package_step() {
  case "$1" in
    install_apt_deps | install_uv) return 0 ;;
    *) return 1 ;;
  esac
}

_converge_package_failed() {
  log_error "Package phase failed - system epoch NOT advanced (see ${MCAPP_BOOTSTRAP_LOG:-bootstrap log})"
}

converge_system_steps() {
  local installed="${1:?installed epoch required}"
  local name since

  # The table is on fd 3, closed for each step: on stdin, a step that reads stdin
  # (apt-get, systemd-run) would eat the remaining rows and they would be skipped.
  while read -r name since <&3; do
    if (( installed < since )); then
      log_info "converge: run ${name} (changed in epoch ${since}, installed ${installed})"
      if _converge_is_package_step "$name"; then
        if ! "$name" 3<&-; then
          _converge_package_failed
          return 1
        fi
      else
        "$name" 3<&-
      fi
    else
      log_info "converge: skip ${name} (changed in epoch ${since}, installed ${installed})"
    fi
  done 3< <(converge_step_table)

  log_info "converge: run ensure_web_frontend (always)"
  if ! ensure_web_frontend; then
    _converge_package_failed
    return 1
  fi
}
