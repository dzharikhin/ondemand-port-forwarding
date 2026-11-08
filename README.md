# ondemandportforward

On-demand reverse SSH tunnels triggered by email. A containerized daemon watches
an IMAP mailbox; a properly authenticated "command letter" makes it open a
reverse SSH tunnel (`ssh -R`) to the host named in the letter, so a remote
machine can reach services on your internal network. The daemon replies by
email with connection details or error logs.

## How it works

1. Daemon polls the mailbox (IMAP IDLE, poll fallback).
2. A letter arrives in one of two forms:
   - **Encrypted** (normal): body is a GPG symmetric `-----BEGIN PGP MESSAGE-----`
     block, encrypted with your shared passphrase (`SECRET`).
   - **Plaintext** (emergency fallback, when gpg is unavailable): plain
     `key: value` body plus a one-time `code:` line. The code burns on first use.
3. Authenticated letter -> tunnel `ssh -R <remote_port>:localhost:<local_port> user@remote`
   opens for a limited TTL (default 30 min).
4. Reply email: success (connect instructions) or failure (last log lines).
   Replies are prefixed `[ondemandportforward]` and never re-processed.

Letter body fields:

| Field | Required | Default | Meaning |
|---|---|---|---|
| `host` | yes | — | remote machine the tunnel goes to |
| `user` | yes | — | ssh login on the remote machine |
| `password` | yes | — | ssh password (masked in all logs/replies) |
| `port` | no | `22` | sshd port on the remote machine |
| `forwards` | no | `2222:22` | comma list `remote_port:local_port` |
| `ttl` | no | `1800` | seconds, 60..86400 |
| `code` | plaintext mode | — | current one-time code |

After the tunnel is up, on the remote machine: `ssh -p 2222 <user>@localhost`
(forward `2222:22`) reaches the ssh of the machine the daemon runs on.

## Build the image

```bash
docker build -t ondemandportforward:latest .
# or via compose:
docker compose build
```

## Configure

```bash
cp .env.example .env
vi .env
```

Required (everything else has defaults):

```ini
IMAP_HOST=imap.example.com
IMAP_USER=user@example.com
IMAP_PASSWORD=...
SMTP_HOST=smtp.example.com
SECRET=long-random-passphrase   # gpg passphrase for command letters
```

Useful optional: `SMTP_USER`/`SMTP_PASSWORD` (default = IMAP creds),
`PLAINTEXT_CODE` (initial one-time code), `STRICT_HEADERS=1` (require
spf/dkim pass).

## Launch (always-on)

Recommended — compose (`restart: unless-stopped` + healthcheck are configured,
container comes back on reboot/crash):

```bash
docker compose up -d
docker compose logs -f
```

Equivalent plain `docker run`:

```bash
docker run -d --name ondemandportforward \
  --restart unless-stopped \
  --env-file .env \
  -v ondemandportforward_data:/data \
  ondemandportforward:latest
```

## State and logs

All state lives in the `/data` volume as plain files, readable outside the
container:

```
imap.json      # mailbox watermark (last_uid, seen UIDs)
replay.json    # replay cache of processed ciphertexts
code.json      # one-time password usage state (armed + burned codes)
sessions.jsonl # session lifecycle log (start/up/retry/ttl_kill/exit/fail)
attempts.jsonl # audit of every email attempt (accept/reject/ignore + reason)
```

```bash
docker compose exec ondemandportforward tail -20 /data/attempts.jsonl
```

Forwards target `localhost` as seen from the daemon. To tunnel into the
services of the machine the container runs on, launch it with `--network host`.

## Sending a command letter

### Encrypted (normal way)

Interactive helper builds and encrypts the letter for you:

```bash
docker compose run --rm ondemandportforward encrypt
```

Paste the printed `-----BEGIN PGP MESSAGE-----` block as the email body
(plain text, not HTML) and send it to your own address.

Manual, on any machine with gpg (passphrase = `SECRET`):

```bash
cat > body.txt <<EOF
host: 203.0.113.10
user: root
password: hunter2
forwards: 2222:22
ttl: 1800
EOF
gpg -a -c --cipher-algo AES256 body.txt
```

### Plaintext fallback (one-time code)

Arm a code first (hidden prompt; old codes die, `--clear` disables the mode):

```bash
docker compose run --rm ondemandportforward set-code
```

Then email a plain `key: value` body plus `code: <the-code>`. The code burns
at acceptance — even a typo burns it; arm a new one to retry. A `code already
used` reply is also the tamper alarm: something reused your code before you did.
Rotate the remote machine's password after using this mode (the letter was
readable to the mail provider).

## Inspecting

```bash
docker compose logs -f                              # daemon log
docker compose run --rm ondemandportforward health  # state files check
docker compose exec ondemandportforward tail -20 /data/attempts.jsonl
docker compose exec ondemandportforward tail -20 /data/sessions.jsonl
```

## Security notes

- `SECRET` (gpg) provides confidentiality + authenticity: providers see only
  ciphertext, forged letters can't decrypt. Exact resends are rejected by the
  replay cache.
- One-time plaintext codes burn on use; live ssh passwords never appear in
  logs or replies.
- Choose a long `SECRET` and keep `/data` backed up — it holds the armed code
  and audit history.
