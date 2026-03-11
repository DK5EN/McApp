# High-Hop Forensics SOP

Runbook for extracting and analyzing `HIGH_HOP_FORENSIC` log entries from production.

## Background

MeshCom firmware officially supports max 4 hops. We observe messages with 5-12 hops in production. Firmware maintainers dispute this. The `HIGH_HOP_FORENSIC` logger in `sqlite_storage.py` captures raw data as evidence whenever a message arrives with >4 stations in its relay path.

## How to run

### 1. Extract log entries from mcapp.local

```bash
ssh mcapp.local 'sudo journalctl -u mcapp --since "7 days ago" --no-pager' | grep HIGH_HOP_FORENSIC
```

For structured JSON output (one JSON object per line):

```bash
ssh mcapp.local 'sudo journalctl -u mcapp --since "7 days ago" --output=json --no-pager' | \
  python3 -c "
import sys, json
for line in sys.stdin:
    try:
        entry = json.loads(line)
        msg = entry.get('MESSAGE', '')
        if 'HIGH_HOP_FORENSIC' in msg:
            print(msg)
    except json.JSONDecodeError:
        pass
"
```

Adjust `--since` as needed (e.g. `"2026-03-02"`, `"30 days ago"`).

### 2. Log format

Each line looks like:

```
HIGH_HOP_FORENSIC hops=7 src=DG1GMY-23 dst=26277 type=msg via=DB0AU-12,DB0HOB-12,DL3MBG-12,OE2XZR-12,DD7MH-11,DB0HOB-12,DB0ED-99 max_hop=None mesh_info=None src_type=lora raw={"src":"DG1GMY-23,DB0AU-12,...","dst":"26277","msg":"...","type":"msg",...}
```

Fields:
- **hops** — number of stations in the relay path (>4 triggers logging)
- **src** — originating callsign
- **dst** — destination (callsign, group number, or `*` for broadcast)
- **type** — `msg`, `pos`, `tele`
- **via** — comma-separated relay path (parsed from raw data)
- **max_hop** — firmware hop-limit header field (from binary BLE frame byte 6, low nibble). `None` for UDP.
- **mesh_info** — firmware mesh-info header field (byte 6, high nibble). `None` for UDP.
- **src_type** — `lora` (UDP from node), `ble_remote` (BLE binary frame), or `ble` (local BLE)
- **raw** — the complete JSON as received before any parsing. This is the primary evidence.

### 3. Report template

Produce a report with:

1. **Summary table**: total count, date range, hop distribution (5/6/7/8/...+), top senders
2. **Per-message detail** (sorted newest first): date, src, dst, type, hop count, full relay path, raw JSON
3. **Relay station frequency**: which stations appear most often in paths
4. **Anomalies**: loops (same station twice in a path), `max_hop` values that contradict observed hops, UDP vs BLE breakdown
5. **Raw evidence block**: for the top 5 highest-hop messages, include the full `raw=` JSON verbatim

### 4. AI prompt reference

To generate a report in a future session, say:

> Read `doc/high-hop-forensics-SOP.md` and follow it. Extract HIGH_HOP_FORENSIC entries from mcapp.local for the last N days and produce the report.

The AI should:
1. SSH to `mcapp.local` and run the extraction command from step 1
2. Parse the output
3. Generate the report per the template in step 3
4. Optionally compare with `doc/messages-mehr-als-4-hops.md` (the baseline from 2026-03-02)

## Source code reference

- Logger location: `src/mcapp/sqlite_storage.py`, search for `HIGH_HOP_FORENSIC`
- Relay path parsing: `src/mcapp/ble_protocol.py` → `split_path()`
- Binary frame header (max_hop/mesh_info): `src/mcapp/ble_protocol.py` → `decode_binary_message()`
