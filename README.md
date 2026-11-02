<div align="center">

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                                                ┃
┃                  ◆ VPS BOT ◆                   ┃
┃              VPS BOT BY SARTHAK34              ┃
┃                   Sᴀʀᴛʜᴀᴋ34                    ┃
┃                      v3.9                      ┃
┃                                                ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

**A Discord bot that creates and manages VPS instances (Docker containers) for your members.**

</div>

## Features
- `/claim` / `/buy` - members pick a NODE and get a VPS with an invite-based system (`-i`, `/i`, `/invites`)
- `/admin_deploy` - admins deploy a VPS with custom RAM / CPU / disk (optional SSH IP + password for Termius)
- `/start` `/stop` `/restart` `/regen_sshx` `/status` `/delete`
- `/rebuild` - reinstall a VPS with another OS (Ubuntu 24.04 / 22.04 / 20.04, Debian 12 / 11)
- Access through **sshx** (browser terminal link sent in DMs)
- Works with **Docker** (each VPS = container) or **LOCAL mode** (no Docker, each VPS = folder + sshx session)

## Install (one command, as root)
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/minecraft015662-svg/vps-bot/main/install.sh)
```
Menu:
```
[1] Install the bot      -> asks Bot Token + Admin ID, writes .env, starts the bot
[2] Uninstall the bot    -> stops and removes everything (optionally the VPS containers)
[3] Bot status & logs
[0] Exit
```
Direct modes: `bash install.sh install` · `bash install.sh uninstall` · `bash install.sh status`

Non-interactive install:
```bash
DISCORD_TOKEN=xxxxx OWNER_IDS=123456789 bash install.sh install
```

## Before you start
1. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications).
2. **Bot** tab -> enable **Server Members Intent** and **Message Content Intent**.
3. Invite it with scopes `bot` + `applications.commands` and the **Manage Server** permission.

## Notes
- Debian / Ubuntu only (apt). Run as root.
- The installer picks the backend automatically: Docker if it works on the machine, otherwise LOCAL mode
  (for example inside a container VPS where Docker cannot run).
- Without systemd (container VPS) the bot runs in the background: `bash /opt/vps-bot/start.sh` / `stop.sh`.
- Never upload `.env` or `vps_db.json` (they are in `.gitignore`).

## Credits
Made by **Sarthak34**.
