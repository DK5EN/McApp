# Release History

## v1.6.3 (2026-03-29)

### Highlights

- Automatic summer/winter time switching: the `--settime` command now sends the
  UTC offset along with the time and auto-detects DST transitions, so MeshCom
  nodes always display the correct local time without manual intervention.

### Backend (MCProxy)

- **[fix]** Send UTC offset with `--settime` and auto-detect DST transitions
- **[chore]** Update dependencies (starlette 1.0, aiohttp 3.13.4, attrs 26.1)

### Frontend (webapp)

- **[chore]** Update dependencies (vue, maplibre-gl, vite, eslint, vue-router)
