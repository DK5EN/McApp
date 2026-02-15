# Bootstrap Deployment Logging

## Overview

The bootstrap script (`mcapp.sh`) now logs deployment events directly to the systemd journal for the `mcapp.service`. This provides visibility into maintenance windows, version upgrades, and deployment completion in the service logs.

## Changes Made

### Modified Files
- `bootstrap/lib/deploy.sh`

### Key Changes

1. **Version Tracking in `deploy_app()`**
   - Captures the old version before deployment starts
   - Captures the new version after deployment completes
   - Exports both versions as environment variables for use during service restart

2. **Deployment Event Logging in `enable_and_start_services()`**
   - Logs maintenance start before stopping the service
   - Logs deployment completion after successful restart
   - Logs initial installation for first-time setups

3. **New Function: `log_deployment_event()`**
   - Uses `systemd-cat` to write directly to the systemd journal
   - Tagged with `mcapp` identifier
   - Logged at `info` priority level

## Log Output Examples

### Upgrade Deployment

When upgrading from one version to another:

```
Feb 15 10:30:14 mcapp mcapp[12345]: [BOOTSTRAP] Stopping service for maintenance and deployment
Feb 15 10:30:14 mcapp mcapp[12345]: [BOOTSTRAP] Current version: v1.01.0
Feb 15 10:30:18 mcapp mcapp[12346]: [BOOTSTRAP] Deployment complete - new version: v1.01.1
Feb 15 10:30:18 mcapp mcapp[12346]: [BOOTSTRAP] Upgraded from v1.01.0 to v1.01.1
```

### Initial Installation

When installing for the first time:

```
Feb 15 10:30:18 mcapp mcapp[12346]: [BOOTSTRAP] Initial installation complete
Feb 15 10:30:18 mcapp mcapp[12346]: [BOOTSTRAP] Installed version: v1.01.0
```

### Same Version Reinstall (Force Mode)

When running with `--force` flag:

```
Feb 15 10:30:14 mcapp mcapp[12345]: [BOOTSTRAP] Stopping service for maintenance and deployment
Feb 15 10:30:14 mcapp mcapp[12345]: [BOOTSTRAP] Current version: v1.01.0
Feb 15 10:30:18 mcapp mcapp[12346]: [BOOTSTRAP] Deployment complete - new version: v1.01.0
```

## Viewing Deployment Logs

### View all deployment events

```bash
sudo journalctl -u mcapp.service | grep BOOTSTRAP
```

### View deployment events from a specific time range

```bash
sudo journalctl -u mcapp.service --since "2026-02-15 09:00" | grep BOOTSTRAP
```

### View recent deployment events

```bash
sudo journalctl -u mcapp.service -n 100 | grep BOOTSTRAP
```

### Follow logs in real-time during deployment

```bash
sudo journalctl -u mcapp.service -f | grep --line-buffered BOOTSTRAP
```

## Technical Details

### Version Detection

Versions are read from `version.html` which contains the full git tag (e.g., `v1.01.1` or `v1.01.1-dev.14`).

**Version file locations** (in priority order):
1. `/var/www/html/webapp/version.html` (deployed webapp)
2. `~/mcapp/webapp/version.html` (bundled in release tarball)

### Logging Implementation

The `systemd-cat` command is used to write directly to the systemd journal:

```bash
systemd-cat -t mcapp -p info <<< "[BOOTSTRAP] Message here"
```

- `-t mcapp`: Tags the message with the `mcapp` identifier
- `-p info`: Sets priority level to `info`
- `<<<`: Here-string for message input

### Event Types

| Event Type | Trigger | Log Messages |
|------------|---------|--------------|
| `MAINTENANCE_START` | Service is about to restart | "Stopping service for maintenance and deployment"<br/>"Current version: vX.Y.Z" |
| `DEPLOYMENT_COMPLETE` | Service restarted successfully | "Deployment complete - new version: vX.Y.Z"<br/>"Upgraded from vA.B.C to vX.Y.Z" (if version changed) |
| `INITIAL_INSTALL` | Service started for first time | "Initial installation complete"<br/>"Installed version: vX.Y.Z" |

## Testing

### Test on production

```bash
# Run deployment with --skip to only update code and restart
sudo ./bootstrap/mcapp.sh --skip

# Check logs for deployment events
sudo journalctl -u mcapp.service --since "1 minute ago" | grep BOOTSTRAP
```

### Test initial install in VM

```bash
# Fresh install
curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | sudo bash

# Check logs
sudo journalctl -u mcapp.service | grep BOOTSTRAP
```

## Benefits

1. **Audit Trail**: Clear record of when deployments occurred and what versions were installed
2. **Troubleshooting**: Helps correlate service issues with deployment events
3. **Monitoring**: Can be parsed by log aggregation tools for deployment tracking
4. **Transparency**: Deployment maintenance windows are clearly logged alongside application logs
5. **Version History**: Easy to see version progression over time

## Future Enhancements

Potential improvements:
- Add deployment duration timing
- Log bootstrap script version used
- Add deployment mode (--force, --dev, etc.)
- Log system resource usage before/after deployment
- Add structured JSON logging option for machine parsing
