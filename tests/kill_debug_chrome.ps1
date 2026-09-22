# kill_debug_chrome.ps1 - kill ONLY the hub's own debug-profile Chrome
# (never touches the user's default-profile Chrome windows).
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object {
    $_.CommandLine -like '*chrome_debug_profile*'
} | ForEach-Object {
    Write-Output ("killing debug chrome PID " + $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Output "done"
