# ==============================================================================
# notifier.py - Native Windows 11 toast notifications
# ==============================================================================
from __future__ import annotations

import subprocess


def notify_user(title: str, message: str) -> str:
    """
    Send a native Windows toast notification (via Windows Runtime from
    PowerShell) to alert the user when builds, tests, or loop iterations finish.
    """
    import sys
    if sys.platform != "win32":
        return "Notifications are supported on Windows only."

    # XML-escape the payload
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    ps_script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$template = @"
<toast><visual><binding template="ToastText02">
<text id="1">{esc(title)}</text>
<text id="2">{esc(message)}</text>
</binding></visual></toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("MCP Workstation Hub").Show($toast)
"""
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Toast notification sent: '{title}'."
    except Exception as e:
        return f"Failed to send notification: {e}"
