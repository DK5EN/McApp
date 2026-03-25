# Release History

## v1.6.2

### Bug Fixes

- **Increase dedup window from 20 to 60 minutes** for LoRa relay messages, reducing
  duplicate message delivery to web clients
- **Fix stale dev slot in production deploy** — repopulate target slot when it contains
  stale development content, preventing upgrade failures
- **Fix upgrade failure when target slot is empty** — handle missing slot gracefully
  during deployment

### Frontend

- **Fade stale station markers** on positions map, with MapLibre inline opacity override
  to ensure correct rendering
- **Fix temp/humidity chart** — align series by timestamp for correct tooltips and add
  `spanGaps` to connect sparse data points
- **Update all dependencies** including Vite 8 major upgrade
- **Fix high severity vulnerability** in `flatted` dependency

### Documentation

- Add SQLite performance analysis notes

### Diagnostics

- Add dedup diagnostic logging to frontend for hunting duplicate messages bug
