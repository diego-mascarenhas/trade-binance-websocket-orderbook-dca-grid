# VPS sudoers (bbo / forge)

**Host:** `bbo`  
**User:** `forge`  
**Captured:** 2026-08-22 (`ls /etc/sudoers.d/` + `sudo -l -U forge`)

This is what is **already applied**. Do not add deploy `cp` / `daemon-reload` / `dca-api` NOPASSWD unless we accept a wider hole. Interactive `./deploy/deploy.sh` can still use password sudo via `(ALL : ALL) ALL`.

## Files in `/etc/sudoers.d/`

| File | Owner | Notes |
|------|--------|--------|
| `90-cloud-init-users` | root `440` | Cloud-init |
| `composer` | root `644` | Host: `composer self-update` |
| `nginx` | root `644` | Host: `service nginx *` |
| `php-fpm` | root `644` | Host: `service php*-fpm reload` |
| **`pump-stall-ctl`** | root `440` · 633 B · 2025-07-31 | **This repo** — Telegram `/pump` |
| `README` | root `440` | Distro snippet |
| `supervisor` | root `644` | Host: `supervisorctl` |

Trading only cares about **`pump-stall-ctl`**. The rest is the shared VPS (nginx / PHP / Composer / Supervisor), not the DCA bot.

## `pump-stall-ctl` (NOPASSWD)

Exact commands `forge` may run as root **without a password**. Used by `telegram_botctl.py` (`sudo -n /bin/systemctl …`) for `/pump status|start|stop|early|strict`.

`/pump auto` needs no sudo: it only writes `.state/vol_override.json`, which the scanner re-reads each cycle.

```sudoers
forge ALL=NOPASSWD: \
  /bin/systemctl start pump-stall-watch, \
  /bin/systemctl stop pump-stall-watch, \
  /bin/systemctl enable pump-stall-watch, \
  /bin/systemctl disable pump-stall-watch, \
  /bin/systemctl start pump-stall-watch-early, \
  /bin/systemctl stop pump-stall-watch-early, \
  /bin/systemctl enable pump-stall-watch-early, \
  /bin/systemctl disable pump-stall-watch-early, \
  /bin/systemctl restart pump-stall-watch, \
  /bin/systemctl restart pump-stall-watch-early, \
  /bin/systemctl is-active pump-stall-watch, \
  /bin/systemctl is-active pump-stall-watch-early, \
  /bin/systemctl is-enabled pump-stall-watch, \
  /bin/systemctl is-enabled pump-stall-watch-early
```

Unit names must match **exactly** (no `.service` suffix in these rules). `systemctl *` is **not** granted here.

### What this allows

| Action | early | strict (`pump-stall-watch`) |
|--------|-------|------------------------------|
| start / stop / restart | yes | yes |
| enable / disable | yes | yes |
| is-active / is-enabled | yes | yes |

### What this does **not** allow (password still required)

- `systemctl daemon-reload`
- `cp` of unit files into `/etc/systemd/system/`
- `systemctl restart dca-api` / `dca-telegram-ctl`
- `dca-spot@*` / `dca-futures@*`
- `journalctl`

So `./deploy/deploy.sh` still prompts for sudo on `cp`, `daemon-reload`, and those restarts. That is intentional.

## Full `sudo -l -U forge` (2026-08-22)

Defaults: `env_reset`, `mail_badpass`, `use_pty`,  
`secure_path=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin`

```
(ALL : ALL) ALL
(root) NOPASSWD: /usr/local/bin/composer self-update*
(root) NOPASSWD: /usr/sbin/service nginx *
(root) NOPASSWD: /usr/sbin/service php8.4-fpm reload
(root) NOPASSWD: /usr/sbin/service php8.3-fpm reload
(root) NOPASSWD: /usr/sbin/service php8.2-fpm reload
(root) NOPASSWD: /usr/sbin/service php8.1-fpm reload
(root) NOPASSWD: /usr/sbin/service php8.0-fpm reload
(root) NOPASSWD: /usr/sbin/service php7.4-fpm reload
(root) NOPASSWD: /usr/sbin/service php7.3-fpm reload
(root) NOPASSWD: /usr/sbin/service php7.2-fpm reload
(root) NOPASSWD: /usr/sbin/service php7.1-fpm reload
(root) NOPASSWD: /usr/sbin/service php7.0-fpm reload
(root) NOPASSWD: /usr/sbin/service php5.6-fpm reload
(root) NOPASSWD: /usr/sbin/service php5-fpm reload
(root) NOPASSWD: /bin/systemctl start|stop|enable|disable|restart|is-active|is-enabled
                 pump-stall-watch and pump-stall-watch-early
                 (see pump-stall-ctl above)
(root) NOPASSWD: /usr/bin/supervisorctl reload
(root) NOPASSWD: /usr/bin/supervisorctl reread
(root) NOPASSWD: /usr/bin/supervisorctl restart *
(root) NOPASSWD: /usr/bin/supervisorctl start *
(root) NOPASSWD: /usr/bin/supervisorctl status *
(root) NOPASSWD: /usr/bin/supervisorctl status
(root) NOPASSWD: /usr/bin/supervisorctl stop *
(root) NOPASSWD: /usr/bin/supervisorctl update *
(root) NOPASSWD: /usr/bin/supervisorctl update
```

`(ALL : ALL) ALL` is **passworded** full sudo (any command). NOPASSWD is only the lines above.

## How Telegram uses it

`telegram_botctl.py` → `_systemctl()` → `sudo -n /bin/systemctl …`  
Needs `!requiretty` or a working PTY. If `/pump` fails with sudo, check `sudo -n systemctl is-active pump-stall-watch-early` as `forge`.

## Recheck on the server

```bash
sudo ls -l /etc/sudoers.d/
sudo cat /etc/sudoers.d/pump-stall-ctl
sudo -l -U forge
sudo -n systemctl is-active pump-stall-watch-early
```

Edit only with `sudo visudo -f /etc/sudoers.d/pump-stall-ctl` (`chmod 440`). Do not `echo` into the file.
