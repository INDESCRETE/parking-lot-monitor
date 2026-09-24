# Setting up a lot computer so it runs unattended

These steps are for the mini PC that lives at a lot, running Ubuntu Server. You do
them once per box. Once they're done, the box starts itself after a power cut,
restarts the app if it crashes, and reboots itself if it ever freezes.

## 1. BIOS: power back on after an outage
In the BIOS (usually Del or F2 at boot), find **"Restore on AC power loss"** or
**"After power failure"** and set it to **Power On**. Without this, the box stays
off after a power cut until someone presses the button.

## 2. Never sleep
```
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

## 3. Install the app as a service
```
sudo useradd --system --create-home parking
sudo git clone <repo> /opt/parking-lot-monitor
sudo chown -R parking: /opt/parking-lot-monitor
cd /opt/parking-lot-monitor && sudo -u parking python3 -m venv .venv
sudo -u parking .venv/bin/pip install -r requirements.txt
sudo cp deploy/parking-lot-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now parking-lot-monitor
```
Check on it with `systemctl status parking-lot-monitor` and view its output with
`journalctl -u parking-lot-monitor -f`.

## 4. Reboot automatically if the computer freezes
In `/etc/systemd/system.conf`, set:
```
RuntimeWatchdogSec=30s
```
then reboot. The hardware watchdog restarts the machine if the operating system
stops responding for 30 seconds.

## 5. Alerts
The first run creates `data/alerts.json` with a private ntfy topic. Subscribe to it
in the ntfy phone app, then open `/health` and press **Send test alert**.

Optional, but strongly recommended for real lots: create a free check at
healthchecks.io (period 1 minute, grace 5 minutes). Paste its ping URL into
`heartbeat_url` in `data/alerts.json`, then restart. That service alerts you while
a lot is still offline (power cut, internet down, dead box), which the box itself
can't do.

Optional email: fill in the `email` section (for Gmail, use an app password, not
your normal password) and set `"enabled": true`.

## 6. Battery backup
Plug the mini PC and the PoE switch into a small UPS. That rides out short blips,
and step 1 covers long outages.

## Note
The app currently listens only on the box itself (127.0.0.1). Reaching the dashboard
from another device (you or a client) is a separate step that still needs to be done,
using Tailscale or a secure tunnel. Don't just open the port to the internet.
