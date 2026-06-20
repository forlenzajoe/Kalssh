# Deploying the edge watcher to a 24/7 Linux server

This runs the watcher on an always-on server so it works even when your own
computer is off. It still only **notifies** (phone via ntfy + logs) — it never
places trades.

## Overview (4 phases)

1. **Create the server** — a small Ubuntu VPS (DigitalOcean ~$6/mo recommended).
2. **Get the code onto it** — via a GitHub repo (simplest) or upload.
3. **Add your Kalshi key** — create `.env` and put the `.pem` on the server.
4. **Install + start** — run `deploy/bootstrap.sh`; it sets up a 24/7 service.

## Phase 1 — Create the server

- Sign up at digitalocean.com. Create a **Droplet**:
  - Image: **Ubuntu 24.04 LTS**
  - Plan: **Basic / Regular**, the **$6/mo** (1 GB RAM) size is plenty.
  - Region: closest to you.
  - Authentication: **Password** (simplest) — set a strong root password.
- Open the droplet's **Console** (web-based terminal) from the DO dashboard.

## Phase 2 — Get the code on the server

Easiest is GitHub (the project contains **no secrets** — `.env` and `*.pem` are
git-ignored):

```bash
# on the server console:
cd ~
git clone https://github.com/<your-user>/<your-repo>.git Kalssh
cd Kalssh
```

(If you prefer not to use GitHub, copy the folder up with `scp` from your PC.)

## Phase 3 — Add your Kalshi key

```bash
cd ~/Kalssh
cp .env.example .env
nano .env          # set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH

# put your private key on the server (paste contents, then Ctrl-D):
cat > ~/kalshi_private_key.pem
# ...paste the .pem text..., then press Enter and Ctrl-D
chmod 600 ~/kalshi_private_key.pem
```

Set `KALSHI_PRIVATE_KEY_PATH=/root/kalshi_private_key.pem` (or your home path) in
`.env`, and keep `mode: live` in `config.yaml`. Your ntfy topic is already set.

## Phase 4 — Install and start

```bash
cd ~/Kalssh
bash deploy/bootstrap.sh
```

That installs dependencies and registers a systemd service that runs 24/7 and
restarts on reboot. Useful commands:

```bash
sudo systemctl status kalshi-watcher     # is it running?
journalctl -u kalshi-watcher -f          # live logs
sudo systemctl restart kalshi-watcher    # after editing .env
```

When a genuine edge appears, your phone gets the same ntfy push as before —
now even while your own computer is off.
