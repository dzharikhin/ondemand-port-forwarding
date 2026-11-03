# On-Demand Port Forwarding — reverse SSH по командному письму

Система в docker-контейнере: слушает IMAP-ящик, по специальному письму
поднимает reverse-ssh-туннель на указанный в письме узел и отвечает письмом
с результатом (параметры соединения или лог ошибки).

## Сценарий

1. Сервис опрашивает сконфигурированный IMAP (IDLE, fallback — poll).
2. Видит письмо установленного формата, проверяет подлинность (см. ниже).
3. По данным письма поднимает reverse ssh на указанный адрес/порт
   (`ssh -R`), авторизация на удалённом узле — login+password (sshpass).
4. Отправляет ответ: успех — параметры подключения; ошибка — последние ~40
   строк лога сессии (пароли замаскированы).
5. Удалённый клиент подключается к своему локальному порту (например
   `ssh -p 2222 user@localhost` на удалённом узле) и попадает на ssh
   внутренней машины.

## Аутентификация писем (защита от подделки)

Два режима, тело письма одинаковое (`key: value`), различается только канал:

### 1. GPG-шифрование — основной режим
- Тело письма = `-----BEGIN PGP MESSAGE-----` (symmetric, `gpg -a -c`,
  AES256).
- Парольная фраза задаётся в конфиге (`SECRET`). Она же — аутентификация:
  без неё нельзя создать корректный шифротекст (MDC/AEAD integrity).
- Провайдеры/ретрансляторы видят только шифротекст: логин/пароль ssh,
  хост и порты скрыты.
- Защита от replay: sha256 шифротекста навсегда кэшируется в sqlite;
  повтор письма (точный) отклоняется. GPG даёт разный шифротекст на каждое
  шифрование, поэтому «изменённых» replay не существует — ключа нет.
- Заголовок `From:` должен совпадать с адресом ящика (слабая sanity-проверка,
  бесплатно). Опционально `STRICT_HEADERS=1` — требовать `spf=pass dkim=pass`
  в `Authentication-Results`.

### 2. Plaintext-fallback — аварийный режим (одноразовый код)
- Для случая, когда gpg недоступен (например, вы уже ssh-нуты в удалённый
  узел и нужно срочно открыть туннель).
- Тело — открытый `key: value` + строка `code: <PLAINTEXT_CODE>`.
- Код **одноразовый**: сгорает в момент приёмки (до попытки туннеля).
  Повторное использование отклоняется («code already used») — это же
  сигнализирует, если код перехватили и сожгли раньше вас.
- Активный код хранится в sqlite (hash). `PLAINTEXT_CODE` в конфиге —
  только начальный seed (применяется, если в базе нет «заряженного» кода).
- Ротация — CLI-инструментом `set-code` (см. ниже), без правки env/restart.
- Остаточный риск (принят): окно в секунды между попаданием письма в ящик и
  обработкой — активный перехватчик может сжечь код первым со своим хостом.
  Ущерб ограничен: одна сессия, TTL, тревожный ответ вам.
- После использования plaintext-режима рекомендуется сменить пароль
  удалённого узла (провайдеры видят письмо).

## Формат тела письма

```
host: 203.0.113.10        # обязательный — удалённый узел
port: 22                  # sshd на удалённом узле (по умолчанию 22)
user: root                # обязательный — ssh-логин на удалённом узле
password: hunter2         # обязательный — ssh-пароль
forwards: 2222:22, 8080:80  # remote:local, по умолчанию "2222:22"
ttl: 1800                 # сек, по умолчанию 1800 (30 минут)
```

- Plaintext-режим добавляет `code: <код>` (шифрованному письмою код не нужен).
- Неизвестные ключи игнорируются. Пароли маскируются во всех логах и ответах.

### Шпаргалка отправителя (обычный режим)

```
$ cat > body.txt <<EOF
host: 203.0.113.10
user: root
password: hunter2
forwards: 2222:22
EOF
$ gpg -a -c --cipher-algo AES256 body.txt   # passphrase = SECRET
```
Вставить вывод (`-----BEGIN PGP MESSAGE----- ...`) в тело письма и отправить
на свой ящик. Либо хелпер: `docker run --rm -it <image> encrypt`.

## Туннель

Для каждого письма:

```
sshpass -e ssh -N -p <port> \
  -o StrictHostKeyChecking=accept-new \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -R <rport>:<INTERNAL_HOST>:<lport> \
  <user>@<host>
```

- Пароль передаётся через env sshpass (не в cmdline).
- `<INTERNAL_HOST>` — цель форвардинга. Важно в docker: `localhost` внутри
  контейнера — это сам контейнер, поэтому compose задаёт
  `INTERNAL_HOST=host.docker.internal` (+ `extra_hosts: host-gateway`),
  чтобы туннель вёл на ssh реальной хост-машины. При `network_mode: host`
  оставьте `localhost`.
- Сопровождение: авто-retry до 3 раз, таймер TTL убивает сессию
  (30 мин по умолчанию, override в письме), stderr пишется в лог сессии
  (пароли маскируются).
- Параллельные сессии допустимы.

## Конфигурация (env)

Обязательные (5):
```
IMAP_HOST= IMAP_USER= IMAP_PASSWORD= SMTP_HOST= SECRET=
```
Необязательные (значения по умолчанию):
```
IMAP_PORT=993  IMAP_TLS_OFF=0  SMTP_PORT=465
SMTP_USER / SMTP_PASSWORD / MAIL_FROM   # по умолчанию = IMAP-креды
PLAINTEXT_CODE=        # seed одноразового кода; пусто = plaintext-режим выключен
TTL_DEFAULT=1800  RETRIES=3  LOG_RETENTION_DAYS=90
STRICT_HEADERS=0  POLL_INTERVAL=60
INTERNAL_HOST=localhost  CONNECT_GRACE=10
```

## CLI инструмента (в том же образе)

- `docker compose run --rm ondemandportforward encrypt` — запросит поля и passphrase,
  прогонит через gpg, выдаст блок для вставки в письмо.
- `docker compose run --rm ondemandportforward set-code` — интерактивно (скрытый ввод)
  заряжает новый одноразовый plaintext-код (старый гарантированно мёртв).
- `docker compose run --rm ondemandportforward set-code --clear` — выключить plaintext-режим.

## Хранилище (sqlite, /data)

```sql
plaintext_code(hash TEXT PRIMARY KEY, set_at, used_at)  -- armed: used_at IS NULL
processed_emails(uid TEXT PRIMARY KEY, seen_at)         -- dedup IMAP UID
replay_cache(body_hash TEXT PRIMARY KEY, seen_at)       -- anti-replay
meta(key TEXT PRIMARY KEY, value)                       -- last_uid watermark и пр.
sessions(id, host, port, ssh_user, forwards, ttl,
         status, started_at, ended_at)
attempts(id, ts, imap_uid, sender, mode,                -- аудит всех попыток
         outcome, reason, session_id)
session_events(id, session_id, ts, event, detail)       -- started/retry/ttl_kill/exit
```

- `attempts` логирует каждую попытку: mode (`encrypted`/`plaintext`),
  outcome (`accepted`/`rejected`/`ignored`), reason (`bad_code`, `code_used`,
  `decrypt_failed`, `bad_sender`, `parse_error`, `replay`, `own_reply`, ...).
- Письма с Subject `[ondemandportforward]...` (собственные ответы) игнорируются — защита от
  цикла ответов.
- Watermark `last_uid` хранится в sqlite: письма, попавшие в ящик до старта,
  не обрабатываются (в т.ч. после рестартов и переподключений).
- Секреты не хранятся; `user@host` хранится (нужен для аудита).
- Ретеншн: `LOG_RETENTION_DAYS` (0 = не чистить), чистка при старте.

## Docker

- Образ: python-3.12-slim + `openssh-client` + `sshpass` + `gnupg`.
- Зависимости: `aioimaplib`, `aiosmtplib`, `python-gnupg`.
- `docker-compose.yml`: env_file, volume `/data` (sqlite).
- Обязательно: синхронизация времени контейнера (TOTP нет, но gpg/логи
  любят точное время; NTP хоста достаточно).

## Структура кода

```
src/ondemandportforward/
  config.py        # env-конфиг, дефолты, валидация
  imap_client.py   # IDLE/poll, dedup по UID
  validator.py     # From-check, выбор режима, приёмка/сжигание кода
  gpg.py           # расшифровка PGP-блока (loopback pinentry)
  parser.py        # тело -> Command(dataclass)
  sessions.py      # надзор за ssh-процессами, TTL, ретраи, логи
  notifier.py      # SMTP-ответы (успех/ошибка, маскирование)
  store.py         # sqlite: схема выше
  cli.py           # encrypt / set-code
  main.py          # сборка, graceful shutdown
tests/             # parser, validator, gpg roundtrip, replay, sessions
```

## Что покрыто и что нет

Покрыто: чтение тела провайдерами (шифрование), подделка письма
(SECRET/MDC + одноразовый код), replay (кэш шифротекста + сгораемый код),
ограничение по времени (TTL).
Не покрыто (принято): компрометация машины с passphrase; качество самой
passphrase (нужна длинная); окно гонки plaintext-кода (см. выше).

## Тестирование

- Unit: parser, validator (обе моды, сжигание кода, replay-кэш), gpg
  roundtrip (реальный gpg в образе/CI), store.
- Integration: greenmail (IMAP/SMTP) + sshd-контейнер → шифрованное письмо
  → туннель поднялся → ответ ушёл; plaintext-письмо → код сгорел →
  повтор отклонён.
