# ==============================================================================
# uia_bridge.ps1 - Resident UI Automation bridge for the mouse runtime.
#
# Spawned ONCE by modules/mouse_runtime.py as a persistent child process.
# Protocol: one JSON object per line on stdin -> one JSON object per line on
# stdout. The pipe stays open, so a UIA query costs milliseconds after warmup
# (no PowerShell cold start per click).
#
# Ops:
#   find   {id, window: "<title substr or empty>", name: "<substr>", limit}
#          -> {ok, id, candidates:[{name,x,y,w,h,invoke,ctype,enabled}], scanned, ms}
#   invoke {id, window, name, index}
#          -> invokes InvokePattern on the Nth name match (ghost click:
#             the control is clicked WITHOUT moving the cursor)
#          -> {ok, id, invoked, name, ms}
# ==============================================================================
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement

function Find-TopWindow([string]$hint) {
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Window)
    $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
    $h = ''
    if ($hint) { $h = $hint.ToLower() }
    foreach ($w in $wins) {
        if (-not $w) { continue }
        $n = $w.Current.Name
        if (-not $n) {
            # fall back to the Win32 title via RuntimeId-free path: use ProcessId
            continue
        }
        if ($h -eq '' -or $n.ToLower().Contains($h)) { return $w }
    }
    return $null
}

function Get-ElementInfo($e) {
    $r = $e.Current.BoundingRectangle
    $hasInvoke = $false
    try {
        $p = $null
        if ($e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $hasInvoke = $true }
    } catch {}
    $hasToggle = $false
    try {
        $tp = $null
        if ($e.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$tp)) { $hasToggle = $true }
    } catch {}
    $hasLegacy = $false
    try {
        $lp = $null
        if ($e.TryGetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern, [ref]$lp)) { $hasLegacy = $true }
    } catch {}
    return @{
        name    = $e.Current.Name
        x       = [int]$r.X; y = [int]$r.Y
        w       = [int]$r.Width; h = [int]$r.Height
        invoke  = $hasInvoke; toggle = $hasToggle; legacy = $hasLegacy
        ctype   = $e.Current.ControlType.ProgrammaticName
        enabled = $e.Current.IsEnabled
    }
}

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ($line.Trim() -eq '') { continue }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try { $req = $line | ConvertFrom-Json } catch {
        Write-Output ('{"ok":false,"error":"bad json"}')
        continue
    }
    $op = "$($req.op)"
    $resp = @{ ok = $false; id = "$($req.id)"; op = $op }

    try {
        if ($op -eq 'ping') {
            $resp.ok = $true
        }
        elseif ($op -eq 'find' -or $op -eq 'invoke') {
            $win = Find-TopWindow "$($req.window)"
            if (-not $win) {
                $resp.error = 'window_not_found'
            } else {
                $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                                    [System.Windows.Automation.Condition]::TrueCondition)
                $pat = "$($req.name)".ToLower()
                $limit = 12
                if ($req.limit) { $limit = [int]$req.limit }
                $matches = New-Object System.Collections.ArrayList
                $elems   = New-Object System.Collections.ArrayList
                $total = $all.Count
                $cap = $total
                if ($cap -gt 6000) { $cap = 6000 }
                for ($i = 0; $i -lt $cap; $i++) {
                    if ($sw.ElapsedMilliseconds -gt 1500) { break }
                    $e = $all.Item($i)
                    if (-not $e) { continue }
                    $n = $e.Current.Name
                    if ($n -and $pat -ne '' -and $n.ToLower().Contains($pat)) {
                        [void]$elems.Add($e)
                        [void]$matches.Add((Get-ElementInfo $e))
                        if ($matches.Count -ge $limit) { break }
                    }
                }
                if ($op -eq 'find') {
                    $resp.ok = ($matches.Count -gt 0)
                    $resp.candidates = $matches
                    $resp.scanned = $cap
                    $resp.window_total = $total
                } else {
                    $idx = 0
                    if ($req.index) { $idx = [int]$req.index }
                    if ($matches.Count -eq 0) {
                        $resp.error = 'element_not_found'
                    } else {
                        if ($idx -ge $matches.Count) { $idx = 0 }
                        $el = $elems[$idx]
                        $invoked = $false
                        $how = ''
                        try {
                            $p = $null
                            if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) {
                                $p.Invoke(); $invoked = $true; $how = 'invoke'
                            }
                        } catch {}
                        if (-not $invoked) {
                            try {
                                $tp = $null
                                if ($el.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$tp)) {
                                    $tp.Toggle(); $invoked = $true; $how = 'toggle'
                                }
                            } catch {}
                        }
                        if (-not $invoked) {
                            try {
                                $lp = $null
                                if ($el.TryGetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern, [ref]$lp)) {
                                    $lp.DoDefaultAction(); $invoked = $true; $how = 'legacy'
                                }
                            } catch {}
                        }
                        $resp.ok = $invoked
                        if ($invoked) {
                            $resp.invoked = $true; $resp.how = $how; $resp.name = $matches[$idx].name
                        } else {
                            $resp.error = 'no_clickable_pattern'
                        }
                    }
                }
            }
        }
        else {
            $resp.error = "unknown_op:$op"
        }
    } catch {
        $resp.error = $_.Exception.Message
    }
    $resp.ms = $sw.ElapsedMilliseconds
    Write-Output ($resp | ConvertTo-Json -Compress -Depth 6)
    [Console]::Out.Flush()
}
