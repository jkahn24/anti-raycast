# ask_claude.ps1 — Anti-Raycast's ". <prompt>" bridge.
# Opens a NEW Claude session in the running Cursor window (Ctrl+Shift+C,
# the default keybinding for a new Claude Code session) and pastes + submits the prompt.
#
# Timing matters: the new session needs a beat before its input takes
# focus — pasting too early sends the keys into the void (bug 2026-07-13).
# Every step is logged to cache\ask_claude.log for diagnosis.
param([Parameter(Mandatory = $true)][string]$Prompt)

$log = Join-Path $PSScriptRoot "..\cache\ask_claude.log"
function Log($m) { Add-Content -Path $log -Value "$(Get-Date -Format 'HH:mm:ss.fff') $m" }

$p = Get-Process Cursor -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle } | Select-Object -First 1
if (-not $p) { Log "FAIL: Cursor not running"; exit 1 }

Set-Clipboard -Value $Prompt
Log "prompt on clipboard ($($Prompt.Length) chars)"

$ws = New-Object -ComObject WScript.Shell
$null = $ws.AppActivate($p.Id)
Start-Sleep -Milliseconds 700
$ws.SendKeys('^+c')                    # new Claude session
Log "sent Ctrl+Shift+C"

Start-Sleep -Milliseconds 3500         # let the session spawn + focus input
$null = $ws.AppActivate($p.Id)         # re-assert focus (it can drift)
Start-Sleep -Milliseconds 300
$ws.SendKeys('^v')                     # paste the prompt
Log "pasted"
Start-Sleep -Milliseconds 600
$ws.SendKeys('{ENTER}')
Log "submitted: $($Prompt.Substring(0, [Math]::Min(60, $Prompt.Length)))"
