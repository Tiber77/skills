# Развёртывание и приёмка

## Подготовка

Проверь `cat /etc/os-release`, `ss -lntup`, running/enabled services и timers systemd, Docker/Podman, `/opt/marzban`, `/var/lib/marzban`, каталоги x-ui, `/root/.acme.sh`, Certbot и `ufw status`. Дополнительно проверь имя администратора, свободные выбранные порты и доступность пакетов через apt-cache. Предоставление сервера для аудита не разрешает удаление найденных приложений.

DNS A должен указывать на VPS; неверная AAAA может сломать HTTP-01. Базовый профиль использует IPv4 для панели и не управляет DNS/firewall провайдера. Внешний firewall должен пропускать TCP 80, 443, SSH-порт и порт панели. При смене SSH временно нужен исходный порт. При HTTP/CDN-проксировании проверь совместимость; обычно нужен DNS only.

Скопируй `scripts/deploy.py`, `scripts/backup.py`, файл публичного ключа и JSON в рабочий каталог на новом VPS. Шаблон JSON находится в `assets/deploy.json.example`: заполни все null-поля параметрами текущего сервера. Поле `reality_target` обязательное, без значения по умолчанию; домен панели — конкретный публичный FQDN. Email должен принадлежать пользователю. Не отправляй приватный SSH-ключ, локальные credentials или старые серверные конфиги в комплекте.

```sh
python3 deploy.py check --config deploy.json
python3 deploy.py prepare --config deploy.json
```

Для автоматического ввода локального пароля sudo используй `--sudo-password-stdin`: подай одну строку через stdin без PTY/echo. Это не пароль SSH. Не передавай его в командной строке, environment shell history или сохраняемом config/state. Скрипт по умолчанию спрашивает пароль через passwd. При наличии секретного канала можно продолжать без нового запроса пользователю в рамках авторизованной установки.

Скрипт оставляет старый SSH, запускает временный ssh-bootstrap на новом порту, включает UFW с обоими портами, получает SSL, фиксирует image digest, создаёт Xray-конфиг, запускает панель и проверяет её локально. `/usr/local/sbin/marzban-deploy` и `/var/lib/marzban-bootstrap/state.json` служат для следующего этапа. Он отказывается перезаписывать старую установку; этот отказ — не повод удалять её данные.

## Смена SSH

Из НОВОГО подключения выбранного администратора по ключу:

```sh
sudo --preserve-env=SSH_CONNECTION python3 /usr/local/sbin/marzban-deploy finalize
```

Создаётся transient rollback timer на 180 секунд, новый sshd_config заменяет основной без Include, ssh.socket отключается, основной ssh.service переходит на новый порт. Конфиги сохраняются для восстановления. Поддерживается local forwarding, root/password SSH запрещены.

В течение 180 секунд открой ЕЩЁ ОДНО новое подключение и выполни:

```sh
sudo --preserve-env=SSH_CONNECTION python3 /usr/local/sbin/marzban-deploy confirm
```

confirm проверяет происхождение сессии от ssh.service, отменяет таймер и закрывает старые SSH-порты в UFW. При неудаче дождись отката. Через консоль/root-сессию доступен `python3 /usr/local/sbin/marzban-deploy rollback`. Не перезагружай VPS до подтверждения — transient timer не переживает reboot. Не подделывай SUDO_USER/SSH_CONNECTION ради обхода проверки.

## Панель и VPN

Официальный shell-установщик не применяется: команды хоста `marzban ...` может не быть. Используй Compose по явному пути:

```sh
sudo docker compose -f /opt/marzban/docker-compose.yml exec marzban marzban-cli admin create --sudo
```

Для неинтерактивной автоматизации проверь `admin create --help` в выбранном образе. В v0.8.4 есть переменная `MARZBAN_ADMIN_PASSWORD`: короткий Python-процесс внутри контейнера может прочитать пароль из stdin, поставить переменную только своего процесса и exec CLI с username/sudo/Telegram/Discord параметрами. Пароль не нужен в Compose/env-файле или docker exec argv.

Панель: `https://DOMAIN:PANEL_PORT/dashboard/`. Созданный пользователем VLESS-профиль должен иметь flow xtls-rprx-vision. Host Settings Address — VPS/домен, SNI — reality_target, fingerprint chrome. Убедись, что ссылки содержат pbk/sid/sni/flow и подписка содержит правильный HTTPS-порт. Изменение существующих Host Settings не должно ломать другие inbound.

## Проверки

- Внешний HTTPS с проверкой цепочки/hostname, затем API-вход без вывода access token.
- Новый вход по ключу и sudo; `sshd -t`, эффективный `sshd -T`, отсутствие старого внешнего SSH listener.
- `ufw status verbose`; учитывай временные REJECT-правила Fail2ban отдельно от разрешённых портов.
- `docker compose ... ps`, `logs --tail=80 marzban`, `fail2ban-client status sshd`, `systemctl list-timers certbot.timer`.
- `certbot renew --cert-name DOMAIN --dry-run --run-deploy-hooks --no-random-sleep-on-renew`. Последний флаг исключает случайное ожидание в неинтерактивном тесте, но не меняет штатное расписание. Hook кратко перезапускает Marzban; строки Docker progress в stderr сами по себе не означают ошибку.
- Для smoke-теста допустим временный Marzban-пользователь и временный Xray-клиент с HTTP/SOCKS listener только на 127.0.0.1. Проверь HTTPS-запрос через VLESS, а не просто открытую 443. Удаляй пользователя, процесс/контейнер и файл ключевых параметров в finally. Проверка клиентом на том же VPS не доказывает доступность из сети пользователя.

## Обслуживание

После `.env` выполняй `docker compose ... up -d --force-recreate marzban`; один restart не обновляет env контейнера. При изменении только сертификатов deploy-hook достаточно restart. На работающем сервере перезапуск прерывает VPN.

`backup.py` архивирует `/opt/marzban`, `/var/lib/marzban`, `/etc/letsencrypt` в `/var/backups/marzban` с ограниченными правами. Перед обновлением сохрани image digest и БД вместе. Если релиз мигрирует схему, откат одного образа недостаточен. Архив не включает полную ОС/SSH/UFW; восстановление проверяется отдельно.

Реальный базовый прогон 05.09.2026: Ubuntu 26.04.1, Marzban v0.8.4 с Xray 24.12.31, сертификат/renewal hook, SSH-переход, внешний HTTPS/API и HTTP 200 через временный REALITY-клиент. Не считать этот состав последним релизом: проверяй текущие первичные источники перед новой установкой.

Источники: [Marzban releases](https://github.com/Gozargah/Marzban/releases), [Marzban config](https://github.com/Gozargah/Marzban/blob/master/config.py), [REALITY](https://github.com/XTLS/REALITY), [Certbot](https://eff-certbot.readthedocs.io/en/stable/using.html), [Ubuntu SSH socket activation](https://discourse.ubuntu.com/t/sshd-now-uses-socket-based-activation-ubuntu-22-10-and-later/30189).
