# ondemandportforward

On-demand reverse SSH tunnels triggered by email: a containerized daemon
watches an IMAP mailbox, and a properly authenticated "command letter" makes
it open a reverse SSH tunnel (`ssh -R`) to the host named in the letter — so
a remote machine can reach services on your internal network — replying by
email with connection details or error logs.

## Quick start

### 1. Configure

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

### 2. Launch

```bash
docker compose up -d
docker compose logs -f
```

Compose builds the image automatically; `restart: unless-stopped` and a
healthcheck are configured, so the container comes back on reboot/crash.

### 3. Send a command letter

Interactive helper builds and encrypts the letter for you:

```bash
docker compose run --rm ondemandportforward encrypt
```

Or manually, on any machine with gpg (symmetric, passphrase = `SECRET`):

```bash
cat > body.txt <<EOF
host: 203.0.113.10
user: root
password: hunter2
forwards: 2222:22
ttl: 1800
EOF
gpg -a -c --cipher-algo AES256 body.txt   # writes body.txt.asc
```

Email the full `-----BEGIN PGP MESSAGE-----` ... `-----END PGP MESSAGE-----`
block as **plain text** to your own address — full rules in
[Email format](#email-format).

### 4. Connect

With the default forward `2222:22`, once the tunnel is up, port `2222` on the
letter's `host` (the mediator) leads back to the sshd of the machine the
daemon runs on (or to whatever `host:port` you named in `forwards:` — the
connect commands stay the same). Two ways in:

Direct — on the mediator itself:

```bash
ssh -p 2222 <your-user-on-daemon-machine>@localhost
```

Intermediate — from anywhere else, using the mediator as a jump host
(one command):

```bash
ssh -J <letter-user>@<letter-host> -p 2222 <your-user-on-daemon-machine>@localhost
```

(`your-user` is an account on the daemon machine — not the letter's ssh user.)

Need another host:port in the daemon machine's local network while connected?
Add a local forward (`-L`) to the connect command — the target is dialed from
the daemon machine, same rule as `forwards:`:

```bash
ssh -J <letter-user>@<letter-host> -p 2222 \
    -L 8080:192.168.1.50:8080 \
    <your-user-on-daemon-machine>@localhost
```

Now `localhost:8080` on your machine leads to `192.168.1.50:8080` in the
daemon machine's network (e.g. `curl http://localhost:8080`).

Session already open? Press `Enter` then `~C` to get the `ssh>` prompt and
type `-L 8080:192.168.1.50:8080` — the forward attaches to the running
connection, no reconnect needed.

## How it works

1. Daemon polls the mailbox(es) (IMAP IDLE, poll fallback).
2. A letter arrives in one of two forms:
   - **Encrypted** (normal): body is a GPG symmetric `-----BEGIN PGP MESSAGE-----`
     block, encrypted with your shared passphrase (`SECRET`).
   - **Plaintext** (emergency fallback, when gpg is unavailable): plain
     `key: value` body plus a one-time `code:` line. The code burns on first use.
3. Authenticated letter -> tunnel `ssh -R <remote_port>:localhost:<local_port> user@remote`
   opens for a limited TTL (default 30 min).
4. Reply email: success (connect instructions) or failure (last log lines).
   Replies are prefixed `[ondemandportforward]` and never re-processed.

### Letter body fields

| Field | Required | Default | Meaning |
|---|---|---|---|
| `host` | yes | — | remote machine the tunnel goes to |
| `user` | yes | — | ssh login on the remote machine |
| `password` | yes | — | ssh password (masked in all logs/replies) |
| `port` | no | `22` | sshd port on the remote machine |
| `forwards` | no | `2222:22` | comma list `remote:local` or `remote:host:local`; host defaults to `localhost` (as seen from the daemon) |
| `ttl` | no | `1800` | seconds, 60..86400 |
| `code` | plaintext mode | — | current one-time code |

### Forward targets

Each `forwards:` item names a listener on the mediator and where it leads back
to, **as dialed from the daemon container**:

| Letter | Meaning | Use when |
|---|---|---|
| `forwards: 2222:22` | mediator:2222 → daemon's `localhost:22` | the daemon really runs with host networking and you want its own sshd |
| `forwards: 2222:192.168.1.124:22` | mediator:2222 → `192.168.1.124:22` | the daemon host's LAN IP (example IP — use yours; works from any container network mode), or a different machine entirely (e.g. a NAS: `2222:192.168.1.50:22`) |

Both forms mix in a comma list:

```
forwards: 2222:22, 8080:192.168.1.50:8080
```

The remote port (left side) must be unique per letter; hostnames are allowed
on the right (`2222:nas.lan:22`). IPv6 literals are not supported in the
target position (colons conflict with the syntax).

See [State and logs](#state-and-logs) for the podman-compose host-networking
caveat that makes the `host:port` form the safe default.

### Email format

Paste the ciphertext into the message body. The email must look like this:

| Part | Requirement |
|---|---|
| From | anything — sender is not checked; the letter itself (SECRET / one-time code) is the authentication |
| To | your own address (the letter must land in the watched mailbox) |
| Subject | ignored (but replies from the daemon are prefixed `[ondemandportforward]` and never re-processed) |
| Body | the full `-----BEGIN PGP MESSAGE-----` ... `-----END PGP MESSAGE-----` block from `body.txt.asc` |

- All status replies (success and failure) always go to `IMAP_USER`, no
  matter who sent the letter — a random sender gets no feedback.

- The letter must land in one of the watched mailboxes (`IMAP_MAILBOX`,
  default `INBOX`). Providers that auto-sort self-sent mail into a subfolder
  (e.g. mail.ru → `INBOX/ToMyself`) will hide it from the daemon — add that
  folder to `IMAP_MAILBOX` (see [Configuration](#configuration)).

- Paste the block as **plain text, not HTML** (switch your mail client to
  plain-text composing; HTML mode can mangle the base64 with soft line breaks
  or NBSP and decryption fails).
- Text before or after the block is ignored — the first PGP block in the body
  wins.
- No `code:` line in this mode; that field is plaintext-fallback only.

Rules to remember:

- **Symmetric** encryption with the `SECRET` passphrase — not public-key.
- Every ciphertext is **single-use**: the replay cache burns the block on the
  first attempt, even a failed one (e.g. wrong `SECRET`, corrupted paste).
  Never resend the same block — re-encrypt; a fresh encryption of the same
  plaintext is new ciphertext and works.

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

## Configuration

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `IMAP_HOST` | — required | IMAP server of the watched mailbox |
| `IMAP_USER` | — required | mailbox login; replies always go to this address |
| `IMAP_PASSWORD` | — required | IMAP password |
| `SMTP_HOST` | — required | SMTP server for replies |
| `SECRET` | — required | gpg passphrase for command letters |
| `IMAP_PORT` | `993` | IMAP port |
| `IMAP_TLS_OFF` | unset | set `1` to disable IMAP TLS |
| `IMAP_MAILBOX` | `INBOX` | folders to watch, comma-separated (one IDLE connection each) |
| `SMTP_PORT` | `465` | SMTP port |
| `SMTP_USER` | `IMAP_USER` | SMTP login |
| `SMTP_PASSWORD` | `IMAP_PASSWORD` | SMTP password |
| `MAIL_FROM` | `SMTP_USER` | From address on replies |
| `PLAINTEXT_CODE` | — | initial one-time code (only applies if none armed yet; prefer `set-code`) |
| `TTL_DEFAULT` | `1800` | default tunnel TTL, seconds |
| `RETRIES` | `3` | extra ssh connect attempts after the first |
| `LOG_RETENTION_DAYS` | `90` | days to keep the state logs |
| `STRICT_HEADERS` | `0` | `1` = require spf/dkim pass |
| `POLL_INTERVAL` | `60` | seconds between IMAP poll-fallback cycles and reconnects |
| `CONNECT_GRACE` | `10` | seconds to wait for ssh to fail before declaring the tunnel up |
| `DATA_DIR` | `./data` | state directory (compose mounts `/data`) |

### Watching multiple mailboxes

`IMAP_MAILBOX` lists the folders to watch. Watch several when letters land in
different folders — e.g. mail.ru sorts self-sent mail into a subfolder while
mail from others goes to `INBOX`:

```ini
IMAP_MAILBOX=INBOX,INBOX/ToMyself
```

### Launch with secrets typed in

To keep secrets off disk entirely (no `IMAP_PASSWORD`/`SECRET`/
`SMTP_PASSWORD`/`PLAINTEXT_CODE` in `.env`), type them in when launching —
each `read -rsp` prompts hidden (no echo), and the value is consumed inline:
never written to disk, shell history, or `.env`:

```bash
IMAP_PASSWORD=$(read -rsp "IMAP password: " p && echo "$p") \
SECRET=$(read -rsp "GPG passphrase (SECRET): " p && echo "$p") \
PLAINTEXT_CODE=$(read -rsp "Initial one-time code (Enter to skip): " p && echo "$p") \
docker compose up -d
```

Notes:

- Typed-in values override `.env` (compose `environment:` beats `env_file:`);
  delete these vars from `.env` so nothing sensitive sits on disk.
- `PLAINTEXT_CODE` seeds a code only when none is armed yet — Enter to skip is
  fine. Re-arm later with `set-code` (preferred anyway).
- Secrets live only in the container's environment: after `docker compose
  down` (or a reboot that recreates the container) you must type them again.
  That's the tradeoff vs. keeping them in `.env`.
- Docker needs `sudo`? Don't write `sudo IMAP_PASSWORD=... docker compose ...`
  — sudo's `env_reset` silently strips the inline assignments. Wrap with
  `env`, which sets them after sudo's environment reset:

  ```bash
  sudo env \
    IMAP_PASSWORD=$(read -rsp "IMAP password: " p && echo "$p") \
    SECRET=$(read -rsp "GPG passphrase (SECRET): " p && echo "$p") \
    docker compose up -d
  ```

- Any other compose command that needs a secret takes the same inline prefix,
  e.g. encrypting a letter when `SECRET` is not in `.env`:

  ```bash
  SECRET=$(read -rsp "GPG passphrase (SECRET): " p && echo "$p") \
  docker compose run --rm ondemandportforward encrypt
  ```

### Plain docker run

Equivalent to the compose setup:

```bash
docker run -d --name ondemandportforward \
  --restart unless-stopped \
  --network host \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  ondemandportforward:latest
```

### Building the image

```bash
docker build -t ondemandportforward:latest .
# or via compose:
docker compose build
```

## State and logs

All state lives in the `./data` bind mount (container path `/data`) as plain
files, readable outside the container:

```
imap.json      # mailbox watermark (last_uid + uidvalidity)
replay.json    # replay cache of processed ciphertexts
code.json      # one-time password usage state (armed + burned codes)
sessions.jsonl # session lifecycle log (start/up/retry/ttl_kill/exit/fail)
attempts.jsonl # audit of every email attempt (accept/reject/ignore + reason)
```

```bash
docker compose exec ondemandportforward tail -20 /data/attempts.jsonl
```

Forwards target `localhost` as seen from the daemon — with real host
networking (`network_mode: host` in docker-compose.yml) this is the daemon
machine itself (the plain `docker run` equivalent needs `--network host`).

Caveat: some runtimes (notably podman-compose) report `network_mode: host`
without actually giving the container the host network namespace, so
`localhost` targets nothing. Verify with
`docker compose exec ondemandportforward cat /proc/net/dev` vs the host, or
skip the guesswork: point the forward at the daemon host's LAN IP directly —
`forwards: 2222:192.168.1.20:22` (works from any namespace).

Tunnel up but connections close instantly? The forward's target host:port
refused the connection — usually no sshd on the target or wrong address.

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
  replay cache. The From header is never trusted — anyone may send a letter,
  only a valid one has any effect.
- One-time plaintext codes burn on use; live ssh passwords never appear in
  logs or replies.
- Choose a long `SECRET` and keep `/data` backed up — it holds the armed code
  and audit history.
