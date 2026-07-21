# Run in elevated PowerShell (管理员)
# Forwards Windows 0.0.0.0:8000 -> current WSL IP:8000 and opens firewall.
$ErrorActionPreference = 'Stop'
$wslIp = (wsl -e bash -lc "hostname -I | awk '{print \$1}'").Trim()
if (-not $wslIp) { throw 'Cannot resolve WSL IP' }
Write-Host "WSL IP = $wslIp"
netsh interface portproxy delete v4tov4 listenport=8000 listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add v4tov4 listenport=8000 listenaddress=0.0.0.0 connectport=8000 connectaddress=$wslIp
netsh advfirewall firewall delete rule name=chatgpt2api-8000 2>$null | Out-Null
netsh advfirewall firewall add rule name=chatgpt2api-8000 dir=in action=allow protocol=TCP localport=8000 | Out-Null
netsh interface portproxy show all
Write-Host "LAN: http://<this-windows-ip>:8000  (e.g. 192.168.124.9 or 10.10.22.181)"
Write-Host "Auth header: Authorization: Bearer <config.json auth-key>"
