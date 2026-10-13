#PRINTER MONITOR — HP/TOSHIBA INK, TONER, PAPER + EMAILS

##INSTALL / UPDATE
Extract all files to a permanent local folder such as C:\Tools\PrinterMonitor.
When updating, replace package files but keep your existing JSON and encrypted
credential files. Rerun setup to select the new email method and refresh tasks.
Do not move the folder after installation.

Double-click **setup_printer_schedule.cmd** for configuration.
Double-click **run_printer_monitor.cmd** for a visible full-report check.
Double-click **start_printer_monitor.cmd** for the menu / first-time setup.
Manual CMD launchers pause so error messages stay visible.

##SETUP
1. Press Enter to detect Python, or enter the actual **python.exe** path.
   A **printer_monitor.py** path is NOT a Python interpreter. Quoted interpreter
   paths are accepted. Setup requires Python 3.10+ and installs pysnmp if needed.
2. Choose ink/toner, paper, or both BEFORE entering targets.
3. Enter comma-separated IPs, ranges or subnets, for example:
   *1.1.1.1-2.2.2.2, 10.0.2.15*
   Ranges include both endpoints; maximum 4,096 unique addresses.
4. Set the SNMP community, low-level threshold and weekly email policy:
   every report OR only low/empty supplies / failed checks.
5. Choose an email method below, enter your sender and recipients.
     *The defaults should suffice; if not, inquire.*
6. Setup submits a test email. Confirm inbox delivery before scheduling.
   It then submits the first actual levels report.

### MSFT EMAIL: OPTION 1 — CLASSIC OUTLOOK
Open classic desktop Outlook and sign in to your Western account first.
Choose option 1 in setup. No SMTP server or domain password is requested.
Setup installs pywin32 if needed. The monitor selects the exact sender account
from the classic Outlook profile; it refuses to send from a different account.
New Outlook / Outlook on the web do not support this COM automation method.
This mode requires your Windows user to remain signed in (screen can be locked).
Email is submitted to Outlook, which queues/transmits it; acceptance alone does
not prove delivery. Check Outbox / Sent Items and confirm the test inbox receipt.
If programmatic sending needs approval every time, unattended sending cannot
be guaranteed; ask WTS about an approved relay. The package does not change
Outlook security policies or bypass organizational controls.

### OPTION 2 — STANDARD SMTP
For providers that permit SMTP username/password or app-password login.
Provide SMTP host, STARTTLS port (usually 587), sender and credentials.
Credentials are Windows-encrypted for your Windows user and this computer.
Western documents OAuth2 for smtp.office365.com:587. This password mode does
not implement OAuth2 and is not a solution to Western's 535 authentication error.
Implicit TLS port 465 is not supported.

### OPTION 3 — RELAY
Defaults to *mailserver*:*25*, without individual SMTP password authentication.
STARTTLS is used if advertised. Some approved relays use an unencrypted local
SMTP connection.
Use a valid sender address that you own. No access request is submitted
by this package; selecting this option does not grant relay permissions.

### SCHEDULE / STARTUP
Friday check: every Friday at 13:00 in the Windows computer's LOCAL TIMEZONE.
Set the computer to Eastern Time for Ontario's 1 p.m.
  *Can be changed in .py config*
Background jobs have no prompts or browser capture tabs. The computer must
be on and connected to the printer network/VPN and its mail service.
Wake / catch-up settings are enabled where Windows/hardware supports them.
  *Win+R - shell:startup*
**Outlook mode always uses a signed-in user session.** Other methods can optionally
run while signed out using a Windows account password, ***not a PIN***.

The optional Startup shortcut in shell:startup sends a COMPLETE report whenever
you sign in, regardless of the Friday low-only email policy. Keep files in their
permanent folder: setup installs a shortcut, not a duplicate script. Startup
means user sign-in rather than boot before sign-in.

### MANAGE
Run now: Start-ScheduledTask -TaskName 'Printer Monitor - Friday 1pm'
Test email: powershell -NoProfile -ExecutionPolicy Bypass -File .\run_printer_monitor.ps1 -TestEmail
Remove Friday task: Unregister-ScheduledTask -TaskName 'Printer Monitor - Friday 1pm'
Remove sign-in check: remove Printer Monitor.lnk from shell:startup.
Change settings: rerun setup_printer_schedule.cmd.
Runtime log: printer_weekly.log. Setup failures: printer_setup_error.log.
If a launcher fails, copy the exact error text left on screen.

### PRINTER READINGS
Ink/toner: SNMP supplies, direct XML/HTML endpoints, optional Toshiba capture.
Paper: SNMP Printer-MIB input trays. Shows percentage / sheets when reported,
EMPTY for zero, and paper-present/unknown when exact levels aren't available.
SNMP needs the correct read-only community and UDP 161 connectivity.
Background toner checks require working SNMP or a direct HTTP endpoint.
Background paper checks require SNMP. The scheduled task cannot use browser
packet capture. Unknown / unavailable readings are preserved and reported.

### INTERACTIVE
python printer_monitor.py
To use the optional original Toshiba capture fallback:
python -m pip install scapy
python printer_monitor.py --check both --capture-toshiba
Windows capture requires Npcap/capture permissions. HTTP capture cannot read
HTTPS traffic. Refresh TopAccess if needed.

### VALIDATION
Parsing, ranges, report policies, Outlook sender selection, missing-account
refusal and SMTP TLS behavior passed mocked checks. No live emails were sent
or printers scanned during development. Windows / Outlook COM and actual mail
transport must be verified on your computer through setup and inbox receipt.

### REFERENCES
Microsoft Outlook SendUsingAccount:
https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.sendusingaccount
