# DX Needed: Wavelog DX Checker

Find out which DX stations in the weekly **ARRL DX Bulletin** are DXCC entities you
still need, checked against your own [Wavelog](https://www.wavelog.org) log.

- **Paste or drop** a bulletin into the web page and see what's needed: new ones,
  ones you've worked but still need a QSL for, and optional band/mode slots.
- **Or let it watch your Gmail:** when a new bulletin arrives it checks it and
  emails you, e.g. *"DX Needed: 2 new ones, 1 to QSL in ARLD045"*. Nothing is sent
  when there's nothing new.

It uses only the Python standard library, in a small Alpine container (about 50 MB,
about 20 MB RAM). Your Wavelog stays private: nothing needs to be exposed to the internet.

## Install

    git clone https://github.com/jnunham/wavelog-dx-checker.git
    cd wavelog-dx-checker
    docker compose up -d --build
    docker logs wavelog-dx-checker        # shows your one-time setup code

Open **http://<your-server>:8080** and follow the setup screen:

1. **Setup code**: the code from `docker logs`. This stops anyone else on your network
   from claiming the app before you do.
2. **Wavelog**: your Wavelog address and an API key (Wavelog → account menu →
   API Keys; read-only is enough). Press *Test connection*.
3. **Email alerts** (optional): your Gmail address and a Gmail **app password**.
   - Turn on 2-Step Verification on your Google account.
   - Create an app password at https://myaccount.google.com/apppasswords
   - Check IMAP is enabled (Gmail → Settings → Forwarding and POP/IMAP).
   - Make sure the ARRL DX Bulletin is emailed to you (ARRL member email settings).
4. **Admin password**: what you'll sign in with from now on.

Everything can be changed later under **Settings**.

## How your details are protected

- Settings are kept in `/data/config.json` on the container's own Docker volume,
  with file mode `0600`, owned by an unprivileged user (the container never runs as root).
- Your Wavelog key and Gmail app password are never sent back to the browser.
  The settings page only shows that they're set.
- The admin password is stored only as a salted PBKDF2-SHA256 hash (600k rounds).
- Sign-in uses an `HttpOnly`, `SameSite=Strict` cookie. Repeated wrong passwords
  lock that address out for a while. Changing the password signs out other browsers.
- The Gmail connection only reads: the folder is opened read-only, and nothing is
  marked, moved or deleted. The app password can't change your Google account
  settings, and you can revoke it any time.
- The container runs with a read-only filesystem, no Linux capabilities and a memory cap.
- Nothing is in this repository except code. Your settings never leave your server.

The Wavelog key and Gmail app password have to be stored in a form the app can read,
because it checks your mail while you're away. They're protected by file permissions
and the Docker volume rather than encryption: encrypting them with a key kept next to
them on the same disk wouldn't add real protection. Keep the server itself secure. If
you open the page from outside your home network, put it behind HTTPS (e.g. a reverse
proxy or Cloudflare Tunnel) and set `COOKIE_SECURE=1`.

### Forgot your admin password?

    docker exec -it wavelog-dx-checker python settings.py reset-password

### Start over completely

    docker compose down -v        # deletes the volume, including all settings

## What the results mean

| Label | Meaning |
|---|---|
| **NEW ONE** | You've never confirmed this DXCC entity |
| **NEED QSL** | Not confirmed, but you've worked a listed station (✓). Chase the card or LoTW |
| **BAND SLOT / MODE SLOT** | You have the entity, but not confirmed on the band (and mode) you picked |

QSL managers ("QSL via EA5GL"), operators' home calls ("F6ABC will be QRV as 5R8XX")
and credit lines ("provided by K1XYZ") are skipped so they don't show up as false needs.

## Updating

    git pull && docker compose up -d --build

Your settings live on the volume, so they survive updates.

## Advanced

Any setting can also be given as an environment variable in `docker-compose.yml`
(then it's fixed and greyed out on the settings page). This is handy with Docker secrets:

| Variable | Setting |
|---|---|
| `WAVELOG_URL`, `WAVELOG_KEY` | Wavelog address and API key (`wl2_...` = API v2, Wavelog 3.1+) |
| `WAVELOG_INSECURE`, `WAVELOG_NO_INDEX_PHP`, `WAVELOG_STATION_IDS` | Advanced Wavelog options |
| `MAIL_USER`, `MAIL_PASSWORD`, `MAIL_TO` | Gmail address, app password, alert recipient |
| `MAIL_CHECK_HOURS`, `MAIL_FOLDER`, `MAIL_SUBJECT`, `MAIL_FROM_FILTER` | When and where to look (defaults: 4, `INBOX`, `ARLD`) |
| `MAIL_BAND`, `MAIL_MODE`, `MAIL_NOTIFY_NONE`, `PUBLIC_URL` | Alert options |
| `MAIL_IMAP_HOST`, `MAIL_SMTP_HOST` | For providers other than Gmail (IMAP SSL 993, SMTP SSL 465) |
| `LOOKUP_DELAY`, `CACHE_MINUTES`, `MAX_CALLS`, `PORT`, `COOKIE_SECURE` | Server tuning |

Test the email alert without waiting or sending anything:

    docker exec wavelog-dx-checker python mailwatch.py --once --dry-run --resend

Command-line use, without the web app:

    WAVELOG_URL=... WAVELOG_KEY=... python3 dxcheck.py bulletin.txt --band 20m --csv needed.csv
