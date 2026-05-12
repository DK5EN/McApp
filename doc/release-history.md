# Release History

## v1.6.9 (2026-05-12)

Maintenance release with a BLE PIN routing fix, an SSE envelope normalization fix on the frontend, and a performance improvement for large message backfills.

### Backend (MCProxy)

- **[fix]** Forward `PATCH /api/ble/pin` correctly to the BLE service (was silently dropped by the proxy router)
- **[chore]** Update dependencies: idna 3.14, sse-starlette 3.4.3

### Frontend (webapp)

- **[fix]** Normalize `mesh:message` SSE envelope from MCProxy (prevented messages from rendering after the v1.6.4 envelope changes)
- **[perf]** Prevent DOM freeze on large message backfills; optimize chat bubble rendering
- **[chore]** Upgrade TypeScript to 6.0.3 and dependencies

