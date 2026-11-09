# Keeping the Streamlit app awake from your AWS Ubuntu server

Same setup as the `supply` repo, pointed at `qrsupply.streamlit.app`. The
unit names (`wake-supply-qr.*`) and path (`/opt/supply_QR`) differ from
supply's (`wake-streamlit.*`, `/opt/supply`), so both can run on the same box.

A plain `curl` does **not** wake a sleeping Streamlit Community Cloud app —
the "Yes, get this app back up!" button has to be clicked in a real browser.
`.github/scripts/wake_streamlit.py` does this with Playwright; the systemd
units here run that same script on your server instead of relying on GitHub
Actions.

## 1. Clone the repo onto the server

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo mkdir -p /opt/supply_QR
sudo chown $USER:$USER /opt/supply_QR
git clone https://github.com/yelrambob/supply_QR.git /opt/supply_QR
cd /opt/supply_QR
python3 -m venv venv
./venv/bin/pip install playwright
./venv/bin/playwright install --with-deps chromium
```

(If supply is already set up on the box, `--with-deps` is already satisfied,
but each venv still needs its own `playwright install chromium`.)

## 2. Install the timer

```bash
sudo cp /opt/supply_QR/deploy/wake-supply-qr.service /etc/systemd/system/
sudo cp /opt/supply_QR/deploy/wake-supply-qr.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wake-supply-qr.timer

# sanity check
sudo systemctl start wake-supply-qr.service   # runs it once, right now
sudo journalctl -u wake-supply-qr.service -n 30 --no-pager
```

The timer fires every 6 hours by default (edit `OnCalendar=` in
`wake-supply-qr.timer` to change that, then `sudo systemctl daemon-reload &&
sudo systemctl restart wake-supply-qr.timer`).

Once this is running reliably you can disable the GitHub Actions version
(`.github/workflows/wake-app.yml`) to avoid double-pinging — delete it or
disable it in the GitHub Actions UI.
