# Remote BLE Implementation Notes

> **Migration Note (2026-02-15):** As of v1.01.1, remote mode is now the only way to access BLE hardware. The legacy local mode described in this document has been removed. All BLE access now goes through the standalone BLE service.

⏺ Implementation complete. Here's a summary of what was created:                
                                                                                
  Created Files                                                                 
                                                                                
  Documentation                                                                 
                                                                                
  - orb-testing.md - OrbStack development workflow documentation                
                                                                                
  BLE Service (standalone FastAPI service)                                      
                                                                                
  - ble_service/src/__init__.py - Package init                                  
  - ble_service/src/ble_adapter.py - Clean D-Bus/BlueZ wrapper (~600 lines)     
  - ble_service/src/main.py - FastAPI REST API + SSE endpoints                  
  - ble_service/pyproject.toml - Dependencies                                   
  - ble_service/mcapp-ble.service - Systemd service file                      
  - ble_service/README.md - API documentation                                   
                                                                                
  BLE Client Abstraction                                                        
                                                                                
  - ble_client.py - Abstract interface + factory function                       
  - ble_client_local.py - Local D-Bus implementation (wraps existing            
  ble_handler)                                                                  
  - ble_client_remote.py - Remote HTTP/SSE client                               
  - ble_client_disabled.py - No-op stub for testing                             
                                                                                
  Modified Files                                                                
                                                                                
  - config_loader.py - Added BLE mode configuration                             
  - config.sample.json - Added BLE mode settings                                
  - C2-mc-ws.py - Uses BLE client abstraction                                   
  - requirements.txt - Added aiohttp, aiohttp-sse-client, sse-starlette         
                                                                                
  Configuration Options                                                         
                                                                                
  {                                                                             
    "BLE_MODE": "local",           // "local" | "remote" | "disabled"           
    "BLE_REMOTE_URL": "",          // URL for remote mode                       
    "BLE_API_KEY": "",             // API key for remote mode                   
    "BLE_DEVICE_NAME": "",         // Auto-connect device name                  
    "BLE_DEVICE_ADDRESS": ""       // Auto-connect MAC address                  
  }                                                                             
                                                                                
  Environment variable overrides:                                               
  - MCAPP_BLE_MODE - Override BLE mode                                        
  - MCAPP_BLE_URL - Override remote URL                                       
  - MCAPP_BLE_API_KEY - Override API key                                      
                                                                                
  Testing Workflow                                                              
                                                                                
  1. OrbStack (Mac) with disabled BLE:                                          
  export MCAPP_BLE_MODE=disabled                                              
  python C2-mc-ws.py                                                            
  2. OrbStack with remote BLE:                                                  
  # On Pi:                                                                      
  cd ble_service && uvicorn src.main:app --host 0.0.0.0 --port 8081             
                                                                                
  # On Mac:                                                                     
  export MCAPP_BLE_MODE=remote                                                
  export MCAPP_BLE_URL=http://pi.local:8081                                   
  python C2-mc-ws.py                                                            
  3. Pi with local BLE (unchanged default behavior):                            
  python C2-mc-ws.py  # Uses local D-Bus/BlueZ   
