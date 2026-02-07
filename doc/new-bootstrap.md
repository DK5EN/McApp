⏺ The McApp Bootstrap Architecture has been implemented. Here's what was      
  created:                                                                      
                                                                                
  Created Files                                                                 
                                                                                
  bootstrap/                                                                    
  ├── mcapp.sh             (320 lines) - Main entry point                       
  ├── lib/                                                                      
  │   ├── detect.sh        (223 lines) - State detection                        
  │   ├── config.sh        (378 lines) - Interactive config & validation        
  │   ├── system.sh        (359 lines) - tmpfs, firewall, services              
  │   ├── packages.sh      (362 lines) - apt + uv package management            
  │   ├── deploy.sh        (280 lines) - Webapp + Python scripts                
  │   └── health.sh        (249 lines) - Health checks                          
  ├── templates/                                                                
  │   ├── config.json.tmpl  - Config template                                   
  │   ├── mcapp.service     - systemd unit file                                 
  │   ├── Caddyfile.tmpl    - Caddy reverse proxy config                        
  │   ├── nftables.conf     - Firewall rules                                    
  │   └── journald.conf     - Volatile journal config                           
  ├── requirements.txt      - Python dependencies                               
  └── README.md            (217 lines) - Documentation                          
                                                                                
  Total: 2,584 lines                                                            
                                                                                
  Key Features Implemented                                                      
  ┌────────────────────────┬──────────────────────────────────────────────┐     
  │        Feature         │                Implementation                │     
  ├────────────────────────┼──────────────────────────────────────────────┤     
  │ Single unified command │ curl ... | sudo bash for all scenarios       │     
  ├────────────────────────┼──────────────────────────────────────────────┤     
  │ State detection        │ fresh / incomplete / upgrade modes           │     
  ├────────────────────────┼──────────────────────────────────────────────┤     
  │ Debian version support │ Auto-detect Bookworm (3.11) vs Trixie (3.14) │     
  ├────────────────────────┼──────────────────────────────────────────────┤     
  │ Interactive prompts    │ Callsign, node, coordinates with validation  │     
  ├────────────────────────┼──────────────────────────────────────────────┤     
  │ SD card protection     │ tmpfs for /var/log, /tmp, volatile journald  │     
  ├────────────────────────┼──────────────────────────────────────────────┤     
  │ Firewall               │ nftables (Trixie) with iptables fallback     │     
  ├────────────────────────┼──────────────────────────────────────────────┤     
  │ uv package manager     │ 10-100x faster than pip                      │     
  ├────────────────────────┼──────────────────────────────────────────────┤     
  │ Idempotent operations  │ Safe to re-run without breaking things       │     
  ├────────────────────────┼──────────────────────────────────────────────┤     
  │ Health checks          │ Services, endpoints, config validation       │     
  └────────────────────────┴──────────────────────────────────────────────┘     
  Usage                                                                         
                                                                                
  # Fresh install, update, or repair - same command                             
  curl -fsSL                                                                    
  https://raw.githubusercontent.com/DK5EN/McAdvChat/main/bootstrap/mcapp.sh | 
  sudo bash                                                                     
                                                                                
  # Other options                                                               
  sudo ./mcapp.sh --check        # Dry-run                                    
  sudo ./mcapp.sh --force        # Force reinstall                            
  sudo ./mcapp.sh --fix          # Repair mode                                
  sudo ./mcapp.sh --reconfigure  # Change config                              
                                                      



migration support for existing installations. Here's how it works: 
                                                                                
  Migration Flow                                                                
                                                                                
  ┌─────────────────────────────────────────────────────────────────┐           
  │                    Old Installation                             │           
  │  ~/venv                    (pip-based Python venv)              │           
  │  /etc/mcapp/config.json (existing config)                   │           
  │  /etc/systemd/system/mcapp.service (points to ~/venv)         │           
  └───────────────────────────────┬─────────────────────────────────┘           
                                  │                                             
                      curl ... | sudo bash                                      
                                  │                                             
                                  ▼                                             
  ┌─────────────────────────────────────────────────────────────────┐           
  │                    Detection Phase                              │           
  │  detect_install_state() → "migrate"                             │           
  │  (found ~/venv but no ~/mcapp-venv)                           │           
  └───────────────────────────────┬─────────────────────────────────┘           
                                  │                                             
                                  ▼                                             
  ┌─────────────────────────────────────────────────────────────────┐           
  │                    Migration Phase                              │           
  │  1. Stop mcapp service                                        │           
  │  2. Preserve old ~/venv (not deleted)                           │           
  │  3. Create new ~/mcapp-venv with uv                           │           
  │  4. Update systemd service paths                                │           
  │  5. Add missing config fields (BLE_ENABLED, etc.)               │           
  └───────────────────────────────┬─────────────────────────────────┘           
                                  │                                             
                                  ▼                                             
  ┌─────────────────────────────────────────────────────────────────┐           
  │                    New Installation                             │           
  │  ~/mcapp-venv            (uv-based Python venv)               │           
  │  /etc/mcapp/config.json (updated with new fields)           │           
  │  /etc/systemd/system/mcapp.service (points to ~/mcapp-venv) │           
  │  + tmpfs, nftables, journald hardening                          │           
  └─────────────────────────────────────────────────────────────────┘           
                                                                                
  Key Points                                                                    
  ┌──────────┬───────────────────────────────────────────┐                      
  │  Aspect  │                 Behavior                  │                      
  ├──────────┼───────────────────────────────────────────┤                      
  │ Config   │ Preserved, new fields added automatically │                      
  ├──────────┼───────────────────────────────────────────┤                      
  │ Old venv │ Kept at ~/venv for rollback safety        │                      
  ├──────────┼───────────────────────────────────────────┤                      
  │ New venv │ Created at ~/mcapp-venv with uv         │                      
  ├──────────┼───────────────────────────────────────────┤                      
  │ systemd  │ Service file updated to new paths         │                      
  ├──────────┼───────────────────────────────────────────┤                      
  │ Prompts  │ None (uses existing config values)        │                      
  └──────────┴───────────────────────────────────────────┘                      
  Dry-Run for Existing Installs                                                 
                                                                                
  Users can check what would happen before migrating:                           
  sudo ./mcapp.sh --check                                                     
                                                                                
  This will show:                                                               
  Current State: migrate                                                        
                                                                                
  Would perform the following actions:                                          
                                                                                
    [MIGRATE] Detected old installation (~/venv)                                
    [MIGRATE] Stop mcapp service                                              
    [MIGRATE] Preserve old venv at ~/venv                                       
    [MIGRATE] Create new venv at ~/mcapp-venv                                 
    [MIGRATE] Update systemd service paths                                      
    [MIGRATE] Add missing config fields                                         
    [SYSTEM] Configure tmpfs, firewall, journald                                
    [PACKAGES] Install uv, update dependencies                                  
    [SERVICES] Restart with new configuration                                   
                                                                                
    Note: Your existing config.json will be preserved.                          
    The old ~/venv will NOT be deleted (manual cleanup if desired). 



  Recommended: Google Forms + Google Sheets                                     
                                                                                
  This is what many open-source projects use:                                   
  1. Create a Google Form with fields (OS, version, etc.)                       
  2. Install script POSTs to the form's public endpoint                         
  3. Responses go to a Google Sheet you control                                 
                                                                                
  curl -s "https://docs.google.com/forms/d/e/FORM_ID/formResponse" \            
    -d "entry.123456=$(uname -s)&entry.789012=$VERSION" > /dev/null             
                                                                                
  No authentication needed, data goes to your Google Sheet. 
