# TLS Remote Access Architecture

## Diagram 1: Base Setup (No TLS — LAN Only)

```mermaid
flowchart TD
    Browser["Browser<br/>(LAN)"]

    Browser -- "HTTP :80" --> LH

    subgraph Pi["Raspberry Pi Zero 2W"]
        LH["lighttpd :80<br/>(static files + proxy)"]
        FA["FastAPI :2981<br/>(SSE + REST API)"]
        MC["McApp Core<br/>(MessageRouter)"]
        UDP["UDP Handler :1799"]

        LH -- "/webapp/ → static files" --> LH
        LH -- "/events → proxy" --> FA
        LH -- "/api/ → proxy" --> FA
        FA --> MC
        MC --> UDP
    end

    UDP -- "UDP :1799" --> Node["MeshCom Node<br/>(LoRa)"]
```

## Diagram 2: TLS Setup (Caddy + Let's Encrypt)

```mermaid
flowchart TD
    Browser["Browser<br/>(Internet)"]
    LE["Let's Encrypt<br/>(ACME DNS-01)"]
    DNS["DNS Provider<br/>(DuckDNS / CF / deSEC)"]

    Browser -- "HTTPS :443" --> Caddy

    subgraph Pi["Raspberry Pi Zero 2W"]
        Caddy["Caddy :443<br/>(TLS termination + DDNS)"]
        LH["lighttpd :80"]
        FA["FastAPI :2981<br/>(127.0.0.1 only)"]
        MC["McApp Core"]
        UDP["UDP :1799"]

        Caddy -- "reverse_proxy" --> LH
        LH -- "/events, /api/" --> FA
        FA --> MC
        MC --> UDP
    end

    Caddy -. "DNS-01 challenge" .-> LE
    Caddy -. "dynamic_dns update" .-> DNS
    UDP -- "UDP" --> Node["MeshCom Node"]

    style Caddy fill:#2d6,stroke:#333,color:#000
    style LE fill:#f90,stroke:#333,color:#000
```

## Diagram 3: Cloudflare Tunnel Alternative

```mermaid
flowchart TD
    Browser["Browser<br/>(Internet)"]
    CFEdge["Cloudflare Edge<br/>(TLS + DDoS + Access)"]

    Browser -- "HTTPS" --> CFEdge

    CFEdge -- "encrypted tunnel<br/>(outbound from Pi)" --> CFD

    subgraph Pi["Raspberry Pi Zero 2W"]
        CFD["cloudflared<br/>(tunnel agent)"]
        LH["lighttpd :80"]
        FA["FastAPI :2981"]
        MC["McApp Core"]

        CFD -- "HTTP" --> LH
        LH -- "/events, /api/" --> FA
        FA --> MC
    end

    style CFEdge fill:#f60,stroke:#333,color:#000
    style CFD fill:#f90,stroke:#333,color:#000
```

## Diagram 4: ssl-tunnel-setup.sh Flow

```mermaid
flowchart TD
    Start["Run ssl-tunnel-setup.sh"] --> PreFlight

    PreFlight["Pre-flight Checks<br/>McApp running?<br/>lighttpd on :80?<br/>Internet access?"]
    PreFlight -- "OK" --> Choose
    PreFlight -- "FAIL" --> Abort["Exit with error"]

    Choose["Choose DNS Provider"]
    Choose --> DuckDNS["DuckDNS"]
    Choose --> CF["Cloudflare"]
    Choose --> DeSEC["deSEC.io"]
    Choose --> CFT["Cloudflare Tunnel"]

    DuckDNS --> Prompt["Enter hostname + token"]
    CF --> Prompt
    DeSEC --> Prompt
    CFT --> PromptCF["Enter tunnel token"]

    Prompt --> InstallCaddy["Download Caddy binary<br/>(with DNS module)"]
    PromptCF --> InstallCFD["Download cloudflared"]

    InstallCaddy --> Configure["Render Caddyfile<br/>Store secrets in caddy.env"]
    InstallCFD --> ConfigureCF["Write cloudflared config<br/>Store credentials"]

    Configure --> Firewall["Update firewall<br/>Open 443, restrict 2981"]
    ConfigureCF --> Firewall

    Firewall --> Services["Start services<br/>Enable on boot"]
    Services --> Health["Health check<br/>Verify cert / tunnel"]
    Health -- "OK" --> Done["Print public URL"]
    Health -- "FAIL" --> Rollback["Show error details"]
```

## Diagram 5: Certificate Lifecycle

```mermaid
sequenceDiagram
    participant Caddy as Caddy (Pi)
    participant DNS as DNS Provider
    participant LE as Let's Encrypt

    Note over Caddy: Startup or renewal (every 60 days)

    Caddy->>LE: Request certificate for hostname
    LE->>Caddy: DNS-01 challenge: set TXT record
    Caddy->>DNS: Create _acme-challenge TXT record
    DNS-->>Caddy: OK
    Note over Caddy: Wait for DNS propagation (~60s)
    Caddy->>LE: Challenge complete
    LE->>DNS: Verify TXT record
    DNS-->>LE: TXT record found
    LE->>Caddy: Issue certificate (valid 90 days)
    Caddy->>DNS: Delete TXT record

    Note over Caddy: Certificate installed, serving HTTPS

    loop Every 5 minutes
        Caddy->>DNS: Update A record if IP changed
    end
```
