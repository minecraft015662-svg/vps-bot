import asyncio
import json
import os
import re
import secrets
import shutil
import sys
import time

import discord
from discord import app_commands


# ---------- .env loader (Pterodactyl / bina systemd ke bhi chalega) ----------
def load_env(name=".env"):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

# =============== CONFIG ===============
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN")
BOT_NAME = os.getenv("BOT_NAME", "SARTHAK OFFICIAL")
IMAGE = "ubuntu:22.04"
BASE_IMAGE = "sarthak-vps-base:latest"   # ek baar banti hai, phir har deploy isi se instant hota hai
VPS_HOSTNAME = os.getenv("VPS_HOSTNAME", "sarthak")   # VPS ke andar prompt / hostname
PIDS_LIMIT = "512"            # fork-bomb protection (Docker mode)
DOCKER_IN_VPS = os.getenv("DOCKER_IN_VPS", "").strip().lower() in ("1", "true", "yes", "on")   # VPS ke andar docker (host pe sysbox chahiye)
SSH_ACCESS = True   # image me sshd rehta hai; SSH IP/password sirf /admin_deploy me ssh_access=True par milta hai
SSH_PORT_MIN = int(os.getenv("SSH_PORT_MIN", "20000"))
SSH_PORT_MAX = int(os.getenv("SSH_PORT_MAX", "29999"))
SYSTEMD = os.getenv("SYSTEMD", "").strip().lower()   # "" = off, "1" = systemd, "priv" = privileged systemd
if SYSTEMD:
    BASE_IMAGE = "sarthak-vps-base-systemd:latest"
MAX_PER_USER = 1              # max VPS per normal user
REQUIRE_VOUCH = True          # /start se pehle /vouch zaroori
DEDUCT_INVITES = True         # buy karne par invites cut hon
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()}
DB_FILE = "vps_db.json"

DEFAULT_PLANS = {
    "1": {"name": "NODE1", "node": "NODE1", "ram": "64g", "cpus": "5", "disk": "100g", "invites": 12},
    "2": {"name": "NODE2", "node": "NODE2", "ram": "64g", "cpus": "5", "disk": "100g", "invites": 12},
    "3": {"name": "NODE3", "node": "NODE3", "ram": "64g", "cpus": "5", "disk": "100g", "invites": 12},
    "4": {"name": "NODE4", "node": "NODE4", "ram": "64g", "cpus": "5", "disk": "100g", "invites": 12},
    "5": {"name": "NODE5", "node": "NODE5", "ram": "64g", "cpus": "5", "disk": "100g", "invites": 12},
}
BLUE, GREEN, RED, PURPLE = 0x3B82F6, 0x2ECC71, 0xE74C3C, 0x9B59B6
LOCK_OK = "🔒 Re-established access credentials (sshx) sent directly to your DMs."
LOCK_FAIL = "⚠️ DM band hai - DMs on karke `/regen_sshx` chalao."
# ======================================

intents = discord.Intents.default()
intents.message_content = True   # Developer Portal me "Message Content Intent" ON karo
intents.members = True    # Developer Portal me "Server Members Intent" ON karo
intents.invites = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


# ---------------- DB ----------------
def load():
    if not os.path.exists(DB_FILE):
        db = {"plans": DEFAULT_PLANS, "invites": {}, "invited": {}, "vps": {}, "admins": []}
        save(db)
        return db
    with open(DB_FILE) as f:
        db = json.load(f)
    db.setdefault("admins", [])
    return db


def save(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


def is_owner(interaction):
    return interaction.user.id in OWNER_IDS


def is_admin(interaction):
    """Owner, /add_admin se add kiye admins, ya server Administrator."""
    if is_owner(interaction) or str(interaction.user.id) in load()["admins"]:
        return True
    return bool(interaction.guild and interaction.user.guild_permissions.administrator)


def admin_only():
    async def pred(interaction: discord.Interaction):
        return is_admin(interaction)
    return app_commands.check(pred)


def owner_only():
    async def pred(interaction: discord.Interaction):
        return is_owner(interaction)
    return app_commands.check(pred)


# ---------------- shell helper ----------------
async def sh(*cmd, timeout=300):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "timeout"
    return proc.returncode, out.decode(errors="ignore").strip()


# ===== BACKEND START =====
# Docker mil jaye to Docker mode, warna LOCAL mode (Pterodactyl jaise panels ke liye).
# Zabardasti set karne ke liye .env me:  VPS_BACKEND=local  ya  VPS_BACKEND=docker
import io
import platform
import signal
import subprocess
import tarfile
import urllib.request

_be = os.getenv("VPS_BACKEND", "").lower()
USE_DOCKER = _be == "docker" or (_be != "local" and shutil.which("docker") is not None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VPS_DIR = os.path.join(BASE_DIR, "vps_data")
BIN_DIR = os.path.join(BASE_DIR, "bin")
PROCS = {}
VM_NOTES = {}   # cid -> warning text (limits enforce nahi ho paye)
VM_SSH = {}         # cid -> {"ssh_port":..., "ssh_pass":...}
_SSH_RESERVED = set()
_PUB_IP = None


def _ssh_args(cid, plan=None):
    """Free host port chuno, random password banao, docker -p flags return karo."""
    plan = plan or {}
    ex = plan.get("ssh_existing")
    if ex:
        VM_SSH[cid] = dict(ex)
        return ["-p", f"{ex['ssh_port']}:22"]
    if not plan.get("ssh"):
        return []
    import socket
    try:
        used = {int(x.get("ssh_port", 0)) for x in load()["vps"].values()}
    except Exception:
        used = set()
    for port in range(SSH_PORT_MIN, SSH_PORT_MAX + 1):
        if port in used or port in _SSH_RESERVED:
            continue
        try:
            with socket.socket() as sk:
                sk.bind(("0.0.0.0", port))
        except OSError:
            continue
        _SSH_RESERVED.add(port)
        VM_SSH[cid] = {"ssh_port": port, "ssh_pass": secrets.token_urlsafe(12)}
        return ["-p", f"{port}:22"]
    return []


async def _public_ip():
    global _PUB_IP
    ip = os.getenv("PUBLIC_IP", "").strip()
    if ip:
        return ip
    if _PUB_IP:
        return _PUB_IP
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            async with sess.get("https://api.ipify.org", timeout=aiohttp.ClientTimeout(total=8)) as r:
                _PUB_IP = (await r.text()).strip()
        return _PUB_IP
    except Exception:
        return "SERVER_IP"


async def dm_ssh(user, cid):
    v = load()["vps"].get(cid, {})
    if not v.get("ssh_port"):
        return False
    ip = await _public_ip()
    e = discord.Embed(title="🖥️ SSH Access (Termius / PuTTY)", color=BLUE)
    e.add_field(name="🌍 Host / IP", value=f"`{ip}`", inline=True)
    e.add_field(name="🔌 Port", value=f"`{v['ssh_port']}`", inline=True)
    e.add_field(name="👤 Username", value="`root`", inline=True)
    e.add_field(name="🔑 Password", value=f"||{v['ssh_pass']}||", inline=False)
    e.add_field(name="⌨️ Command", value=f"```ssh root@{ip} -p {v['ssh_port']}```", inline=False)
    e.add_field(name="\u200b", value="Password kisi se share mat karo.", inline=False)
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    try:
        await user.send(embed=e)
        return True
    except discord.Forbidden:
        return False


# ---------- LOCAL backend (bina Docker) ----------
def sshx_path():
    return shutil.which("sshx") or os.path.join(BIN_DIR, "sshx")


def _ensure_sshx():
    """sshx binary na ho to download karo. Error string ya None return."""
    if os.path.exists(sshx_path()):
        return None
    arch = {"x86_64": "x86_64", "amd64": "x86_64",
            "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine().lower())
    if not arch:
        return f"CPU ({platform.machine()}) supported nahi. sshx binary khud bin/sshx me daalo."
    url = f"https://s3.amazonaws.com/sshx/sshx-{arch}-unknown-linux-musl.tar.gz"
    try:
        os.makedirs(BIN_DIR, exist_ok=True)
        data = urllib.request.urlopen(url, timeout=60).read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            for m in t.getmembers():
                if m.isfile() and os.path.basename(m.name) == "sshx":
                    with open(os.path.join(BIN_DIR, "sshx"), "wb") as f:
                        f.write(t.extractfile(m).read())
        os.chmod(os.path.join(BIN_DIR, "sshx"), 0o755)
    except Exception as e:
        return (f"sshx download fail: {e}. Manually 'sshx' binary download karke "
                f"{BIN_DIR}/sshx me rakho (chmod +x).")
    return None


def _vdir(cid):
    return os.path.join(VPS_DIR, cid)


def _pidfile(cid):
    return os.path.join(_vdir(cid), ".sshx.pid")


def _logfile(cid):
    return os.path.join(_vdir(cid), ".sshx.log")


def _read_pid(cid):
    try:
        with open(_pidfile(cid)) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except Exception:
        return False


def _kill(cid):
    pid = _read_pid(cid)
    if pid and _alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            pass
        for _ in range(10):
            if not _alive(pid):
                break
            time.sleep(0.2)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                pass
    proc = PROCS.pop(cid, None)
    if proc:
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
    try:
        os.remove(_pidfile(cid))
    except Exception:
        pass


def _local_start(cid):
    d = _vdir(cid)
    if not os.path.isdir(d):
        return "VPS folder nahi mila."
    binary = sshx_path()
    if not os.path.exists(binary):
        return "sshx binary nahi mili."
    _kill(cid)
    bashrc = os.path.join(d, ".bashrc")
    if not os.path.exists(bashrc):
        with open(bashrc, "w") as f:
            f.write(f'export HOSTNAME={VPS_HOSTNAME}\n'
                    f'PS1="root@{VPS_HOSTNAME}:\\w# "\n')
    shell = shutil.which("bash") or "/bin/sh"
    # Bot ke env variables (token etc.) VPS shell ko nahi diye jaate.
    env = {"HOME": d, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "TERM": "xterm-256color", "SHELL": shell, "USER": "root",
           "HOSTNAME": VPS_HOSTNAME, "LANG": "C.UTF-8"}
    with open(_logfile(cid), "wb") as log:
        proc = subprocess.Popen([binary, "-q"], cwd=d, env=env, stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    PROCS[cid] = proc
    with open(_pidfile(cid), "w") as f:
        f.write(str(proc.pid))
    return None


def _du(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ---------- DOCKER backend ----------
_BASE_IMAGE_READY = False
_BASE_IMAGE_LOCK = asyncio.Lock()

SETUP_CMD = (
    "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
    "curl ca-certificates procps sudo nano htop git openssh-client && "
    "curl -sSf https://sshx.io/get | sh && "
    f"echo 'PS1=\"root@{VPS_HOSTNAME}:\\w# \"' >> /root/.bashrc && "
    f"echo 'export HOSTNAME={VPS_HOSTNAME}' >> /root/.bashrc"
)


if SYSTEMD:
    SETUP_CMD = SETUP_CMD.replace(
        "curl ca-certificates procps sudo nano htop git openssh-client",
        "curl ca-certificates procps sudo nano htop git openssh-client systemd systemd-sysv dbus iproute2 iputils-ping"
    ) + (" && systemctl mask systemd-udevd.service systemd-udev-trigger.service systemd-modules-load.service"
         " sys-kernel-config.mount sys-kernel-debug.mount sys-kernel-tracing.mount getty.target console-getty.service")


if SSH_ACCESS:
    BASE_IMAGE = BASE_IMAGE.replace(":latest", "-ssh:latest")
    SETUP_CMD = SETUP_CMD.replace("openssh-client", "openssh-client openssh-server", 1) + (
        " && mkdir -p /run/sshd /etc/ssh/sshd_config.d"
        " && echo 'PermitRootLogin yes' > /etc/ssh/sshd_config.d/99-root.conf"
        " && echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config.d/99-root.conf")


if DOCKER_IN_VPS:
    BASE_IMAGE = BASE_IMAGE.replace(":latest", "-dind:latest")
    SETUP_CMD = SETUP_CMD.replace(
        "openssh-client", "openssh-client docker.io systemd systemd-sysv dbus iproute2 iptables", 1
    ) + (" && systemctl mask systemd-udevd.service systemd-udev-trigger.service systemd-modules-load.service"
         " sys-kernel-config.mount sys-kernel-debug.mount sys-kernel-tracing.mount getty.target console-getty.service"
         " && systemctl enable docker")


OS_CHOICES = {
    "ubuntu24": ("Ubuntu 24.04", "ubuntu:24.04"),
    "ubuntu22": ("Ubuntu 22.04", "ubuntu:22.04"),
    "ubuntu20": ("Ubuntu 20.04", "ubuntu:20.04"),
    "debian12": ("Debian 12", "debian:12"),
    "debian11": ("Debian 11", "debian:11"),
}
DEFAULT_OS = "ubuntu22"
_READY_IMAGES = set()


def _os_tag(os_key):
    if not os_key or os_key == DEFAULT_OS or os_key not in OS_CHOICES:
        return BASE_IMAGE
    return BASE_IMAGE.replace(":latest", f"-{os_key}:latest")


async def _docker_ensure_base_image(os_key=None):
    """Har OS ki base image ek hi baar banti hai; uske baad deploy/rebuild seconds me hota hai."""
    tag = _os_tag(os_key)
    src = OS_CHOICES[os_key][1] if (os_key in OS_CHOICES and tag != BASE_IMAGE) else IMAGE
    if tag in _READY_IMAGES:
        return None
    async with _BASE_IMAGE_LOCK:
        if tag in _READY_IMAGES:
            return None
        rc, out = await sh("docker", "image", "inspect", tag)
        if rc == 0:
            _READY_IMAGES.add(tag)
            return None
        tmp = secrets.token_hex(4)
        rc, out = await sh("docker", "run", "-d", "--name", tmp, src, "sleep", "infinity")
        if rc != 0:
            return out[:1500]
        rc, out = await sh("docker", "exec", tmp, "bash", "-c", SETUP_CMD, timeout=900)
        if rc != 0:
            await sh("docker", "rm", "-f", tmp)
            return out[-1500:]
        rc, out = await sh("docker", "commit", tmp, tag)
        await sh("docker", "rm", "-f", tmp)
        if rc != 0:
            return out[:1500]
        _READY_IMAGES.add(tag)
        return None


def _fake_proc_args(cid, plan):
    # Container ke andar free/neofetch/htop me plan wali RAM aur CPU dikhe (host ki nahi)
    import os, math, re as _re
    try:
        m = _re.fullmatch(r"(\d+(?:\.\d+)?)([mgt])", str(plan["ram"]).strip().lower())
        mult = {"m": 1024, "g": 1024 ** 2, "t": 1024 ** 3}
        total = int(float(m.group(1)) * mult[m.group(2)])  # kB
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakeproc", cid)
        os.makedirs(d, exist_ok=True)
        used = min(150 * 1024, total // 4)
        free = total - used
        rows = [("MemTotal", total), ("MemFree", free), ("MemAvailable", free),
                ("Buffers", 0), ("Cached", 0), ("SwapCached", 0), ("Active", used),
                ("Inactive", 0), ("SwapTotal", 0), ("SwapFree", 0), ("Dirty", 0),
                ("Shmem", 0), ("SReclaimable", 0)]
        with open(os.path.join(d, "meminfo"), "w") as f:
            f.write("".join(f"{k + ':':<16}{v:>10} kB\n" for k, v in rows))
        args = ["-v", f"{d}/meminfo:/proc/meminfo:ro"]
        n = max(1, math.ceil(float(plan["cpus"])))
        try:
            blocks = [b for b in open("/proc/cpuinfo").read().split("\n\n") if b.strip()]
            out = []
            for i, b in enumerate(blocks[:n]):
                out.append(_re.sub(r"(?m)^processor\s*:.*$", f"processor\t: {i}", b))
            if out:
                with open(os.path.join(d, "cpuinfo"), "w") as f:
                    f.write("\n\n".join(out) + "\n\n")
                args += ["-v", f"{d}/cpuinfo:/proc/cpuinfo:ro"]
        except Exception:
            pass
        return args
    except Exception:
        return []


def _init_cmd():
    return ["/sbin/init"] if SYSTEMD else ["sleep", "infinity"]


def _systemd_args():
    if not SYSTEMD:
        return []
    common = ["--stop-signal", "SIGRTMIN+3", "--tmpfs", "/run", "--tmpfs", "/run/lock", "--tmpfs", "/tmp"]
    if SYSTEMD == "priv":
        return common + ["--privileged", "--cgroupns=host"]
    return common + ["--cgroupns=host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw"]


def _cpu_cap(c):
    n = os.cpu_count() or 1
    try:
        return str(n) if float(c) > n else str(c)
    except Exception:
        return str(c)


def _dind_args():
    if not DOCKER_IN_VPS:
        return []
    return ["--runtime=sysbox-runc", "--stop-signal", "SIGRTMIN+3"]


def _dind_init(default):
    return ["/sbin/init"] if DOCKER_IN_VPS else default


async def _docker_create(plan):
    err = await _docker_ensure_base_image(plan.get("os"))
    if err:
        return None, f"Base image build fail:\n{err}"
    cid = plan.get("cid") or secrets.token_hex(6)
    base = ["docker", "run", "-d", "--name", cid,
            "--hostname", VPS_HOSTNAME, "-w", "/root", "-u", "0:0",
            "--memory", plan["ram"], "--cpus", _cpu_cap(plan["cpus"]), "--pids-limit", PIDS_LIMIT,
            "--restart", "unless-stopped"] + _dind_args() + _ssh_args(cid, plan) + ([] if DOCKER_IN_VPS else _systemd_args()) + ([] if DOCKER_IN_VPS else _fake_proc_args(cid, plan))
    disk = plan.get("disk")
    if disk:
        rc, out = await sh(*base, "--storage-opt", f"size={disk}", _os_tag(plan.get("os")), *_dind_init(_init_cmd()))
        if rc != 0 and ("storage-opt" in out.lower() or "quota" in out.lower()):
            # Host pe disk quota (overlay2 + xfs pquota) nahi hai -> bina disk limit ke banao
            await sh("docker", "rm", "-f", cid)
            VM_NOTES[cid] = "Disk limit sirf label hai (is host pe storage quota support nahi)."
            rc, out = await sh(*base, _os_tag(plan.get("os")), *_dind_init(_init_cmd()))
    else:
        rc, out = await sh(*base, _os_tag(plan.get("os")), *_dind_init(_init_cmd()))
    if rc != 0:
        return None, out[:1500]
    if SSH_ACCESS and cid in VM_SSH:
        pw = VM_SSH[cid]["ssh_pass"]
        await sh("docker", "exec", cid, "bash", "-c",
                 f"mkdir -p /run/sshd; echo root:{pw} | chpasswd; pgrep -x sshd >/dev/null || /usr/sbin/sshd")
    return cid, None


# ---------- Unified API (baaki bot sirf yehi use karta hai) ----------
async def vm_create(plan):
    if USE_DOCKER:
        return await _docker_create(plan)
    err = await asyncio.to_thread(_ensure_sshx)
    if err:
        return None, err
    cid = secrets.token_hex(6)
    os.makedirs(_vdir(cid), exist_ok=True)
    VM_NOTES[cid] = "Local mode: RAM/CPU/Disk sirf label hain, enforce nahi hote."
    return cid, None


async def vm_start(cid):
    if USE_DOCKER:
        await sh("docker", "start", cid)


async def vm_stop(cid):
    if USE_DOCKER:
        await sh("docker", "stop", cid)
    else:
        await asyncio.to_thread(_kill, cid)


async def vm_restart(cid):
    if USE_DOCKER:
        await sh("docker", "restart", cid)
    else:
        await asyncio.to_thread(_kill, cid)


async def vm_rm(cid):
    if USE_DOCKER:
        await sh("docker", "rm", "-f", cid)
    else:
        await asyncio.to_thread(_kill, cid)
        shutil.rmtree(_vdir(cid), ignore_errors=True)


async def vm_running(cid):
    if USE_DOCKER:
        rc, out = await sh("docker", "inspect", "-f", "{{.State.Running}}", cid)
        return rc == 0 and out.strip() == "true"
    pid = _read_pid(cid)
    return bool(pid and _alive(pid))


async def vm_stats(cid):
    if USE_DOCKER:
        _, out = await sh("docker", "stats", "--no-stream", "--format",
                          "CPU: {{.CPUPerc}} | RAM: {{.MemUsage}}", cid)
        return out or "n/a"
    mb = await asyncio.to_thread(_du, _vdir(cid))
    return f"Disk used: {mb / 1048576:.1f} MB (local mode)"


async def vm_running_count(ids):
    if USE_DOCKER:
        _, out = await sh("docker", "ps", "-q")
        return len(out.split()) if out else 0
    return sum([1 for c in ids if await vm_running(c)])


async def get_sshx_link(cid):
    """VPS start karo, sshx (re)start karo, naya link return karo."""
    if USE_DOCKER:
        await sh("docker", "start", cid)
        if load()["vps"].get(cid, {}).get("ssh_port"):
            await sh("docker", "exec", cid, "bash", "-c", "mkdir -p /run/sshd; pgrep -x sshd >/dev/null || /usr/sbin/sshd")
        await sh("docker", "exec", cid, "bash", "-c", "pkill sshx || true; rm -f /tmp/sshx.log")
        await sh("docker", "exec", "-d", cid, "bash", "-c", "nohup sshx -q > /tmp/sshx.log 2>&1 &")
        reader = lambda: sh("docker", "exec", cid, "cat", "/tmp/sshx.log")
    else:
        if await asyncio.to_thread(_local_start, cid):
            return None

        async def reader():
            try:
                with open(_logfile(cid), errors="ignore") as f:
                    return 0, f.read()
            except Exception:
                return 1, ""
    for _ in range(20):
        await asyncio.sleep(1.5)
        _, log = await reader()
        m = re.search(r"https://sshx\.io/s/\S+", log)
        if m:
            return m.group(0)
    return None


async def is_running(cid):
    return await vm_running(cid)
# ===== BACKEND END =====


# ---------------- Resource parsing ----------------
def parse_size(value, label, units="mg"):
    v = value.strip().lower().replace("gb", "g").replace("mb", "m").replace("tb", "t")
    if not re.fullmatch(rf"\d+(\.\d+)?[{units}]", v):
        raise ValueError(f"{label} ka format galat hai. Example: 512m, 2g, 8g")
    return v


def parse_cpus(value):
    try:
        f = float(value)
    except ValueError:
        raise ValueError("CPU ka format galat hai. Example: 0.5, 1, 2")
    if not 0.1 <= f <= 64:
        raise ValueError("CPU 0.1 se 64 ke beech hona chahiye.")
    return str(int(f)) if f == int(f) else str(f)


def norm_plan(plan):
    p = dict(plan)
    p.setdefault("disk", "10g")
    p.setdefault("invites", 0)
    return p


def resolve_plan(db, plan_number, ram, cpus, disk, node):
    """Plan ya custom specs se final plan dict banao. Return (plan, error)."""
    try:
        if plan_number:
            base = db["plans"].get(plan_number)
            if base is None:
                return None, "❌ Plan nahi mila. `/plans` dekho."
            plan = norm_plan(base)
        else:
            if not (ram and cpus and disk):
                return None, "❌ Ya to `plan_number` do, ya custom ke liye `ram`, `cpus` aur `disk` teeno do."
            plan = {"name": "Custom", "node": "CUSTOM", "invites": 0}
        custom = False
        if ram:
            plan["ram"] = parse_size(ram, "RAM"); custom = True
        if cpus:
            plan["cpus"] = parse_cpus(cpus); custom = True
        if disk:
            plan["disk"] = parse_size(disk, "Disk", units="mgt"); custom = True
        if node:
            plan["node"] = node.strip().upper()[:20]
        if custom and plan_number:
            plan["name"] = f"{plan['name']} (custom)"
        return plan, None
    except ValueError as e:
        return None, f"❌ {e}"


def res_str(v):
    if not v.get("ram"):
        return None
    s = f"`{v['ram'].upper()} RAM` • `{v.get('cpus', '?')} CPU` • `{v.get('disk', '?').upper()} Disk`"
    if v.get("note"):
        s += f"\n⚠️ {v['note']}"
    return s


# ---------------- UI helpers ----------------
def bar(pct):
    filled = pct // 10
    return f"`[{'█' * filled}{'░' * (10 - filled)}]` {pct}%"


def vps_embed(title, text, pct, *, color=BLUE, v=None, cid=None, note=None):
    e = discord.Embed(title=title, color=color)
    e.description = text if pct is None else f"{text}\n{bar(pct)}"
    if v is not None and cid:
        e.add_field(name="👤 Owner Tag", value=f"<@{v['owner']}>")
        e.add_field(name="📦 Container ID", value=f"`{cid}`")
        e.add_field(name="🌐 Cluster Node", value=f"`{v['node']}`")
        r = res_str(v)
        if r:
            e.add_field(name="💾 Resources", value=r, inline=False)
    if note:
        e.add_field(name="\u200b", value=note, inline=False)
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    return e


class Panel:
    """Ek hi message ko progress bar ke saath update karta rehta hai."""

    def __init__(self, interaction, title, cid=None, v=None):
        self.i, self.title, self.cid, self.v = interaction, title, cid, v
        self.msg = None

    async def show(self, pct, text, *, color=BLUE, done=False, note=None):
        e = vps_embed(self.title, text, pct, color=color, note=note,
                      v=self.v if done else None, cid=self.cid if done else None)
        try:
            if self.msg is None:
                self.msg = await self.i.followup.send(embed=e, wait=True)
            else:
                await self.msg.edit(embed=e)
        except discord.HTTPException as ex:
            print("panel update error:", ex)

    async def fail(self, text):
        await self.show(None, f"❌ {text}", color=RED)

    async def run(self, coro, start, text, cap=90, step=7):
        """Coroutine chalte waqt bar dheere dheere badhta hai."""
        task = asyncio.ensure_future(coro)
        pct = start
        await self.show(pct, text)
        while True:
            done, _ = await asyncio.wait({task}, timeout=3)
            if done:
                break
            pct = min(cap, pct + step)
            await self.show(pct, text)
        return task.result()


async def _dm_link_orig(user, link, cid, title="🔑 Your Fresh Remote Access Routes", node=None):
    e = discord.Embed(title=title, color=GREEN)
    e.add_field(name="📦 Container ID", value=f"`{cid}`", inline=True)
    e.add_field(name="🌐 Cluster Node", value=f"`{node or '-'}`", inline=True)
    e.add_field(name="🔗 sshx Web Link:", value=f"```{link}```", inline=False)
    e.add_field(name="\u200b", value=f"root@{VPS_HOSTNAME} shell milega. Link kisi se share mat karo.",
                inline=False)
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    try:
        await user.send(embed=e)
        return True
    except discord.Forbidden:
        return False


async def dm_link(user, link, cid, title="🔑 Your Fresh Remote Access Routes", node=None):
    ok = await _dm_link_orig(user, link, cid, title, node)
    if SSH_ACCESS:
        await dm_ssh(user, cid)
    return ok


async def cid_autocomplete(interaction: discord.Interaction, current: str):
    db = load()
    out = []
    for c, v in db["vps"].items():
        if (v["owner"] == str(interaction.user.id) or is_admin(interaction)) and current.lower() in c:
            out.append(app_commands.Choice(name=f"{c} ({v['plan']})", value=c))
    return out[:25]


async def plan_autocomplete(interaction: discord.Interaction, current: str):
    db = load()
    return [app_commands.Choice(name=f"{k}: {p['name']} ({p['ram']})", value=k)
            for k, p in db["plans"].items()][:25]


async def check_owner(interaction, cid):
    """Owner/admin check. Fail par ephemeral error bhejta hai aur None return karta hai."""
    v = load()["vps"].get(cid)
    if not v or (v["owner"] != str(interaction.user.id) and not is_admin(interaction)):
        await interaction.response.send_message("❌ Ye container tumhara nahi hai ya exist nahi karta.", ephemeral=True)
        return None
    return v


# ---------------- Invite tracking ----------------
invite_cache = {}


async def cache_guild(g):
    try:
        invite_cache[g.id] = {i.code: i.uses for i in await g.invites()}
    except discord.Forbidden:
        pass


@bot.event
async def on_ready():
    for g in bot.guilds:
        await cache_guild(g)
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name=f"{BOT_NAME} • VPS"))
    await tree.sync()
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message):
    # Prefix command: -i  (ya -i @user) -> Invite log
    if message.author.bot or message.guild is None:
        return
    parts = message.content.strip().split()
    if not parts or parts[0].lower() != "-i":
        return
    target = message.mentions[0] if message.mentions else message.author
    db = load()
    uid = str(target.id)
    n = db["invites"].get(uid, 0)
    st = db.get("stats", {}).get(uid, {})
    e = discord.Embed(title="Invite log", color=0x5DD6C0)
    e.description = (f"**\u00bb {target.display_name}** has **{n}** invites\n\n"
                     f"**Joins :** {st.get('joins', 0)}\n**Left :** {st.get('left', 0)}\n"
                     f"**Fake :** {st.get('fake', 0)}\n**Rejoins :** {st.get('rejoins', 0)}")
    e.set_thumbnail(url=target.display_avatar.url)
    e.set_footer(text=f"Requested by {message.author.name}")
    e.timestamp = discord.utils.utcnow()
    await message.reply(embed=e, mention_author=False)


def _inv_stat(db, uid):
    return db.setdefault("stats", {}).setdefault(str(uid), {"joins": 0, "left": 0, "fake": 0, "rejoins": 0})


@bot.event
async def on_invite_create(inv):
    invite_cache.setdefault(inv.guild.id, {})[inv.code] = inv.uses


@bot.event
async def on_invite_delete(inv):
    invite_cache.get(inv.guild.id, {}).pop(inv.code, None)


@bot.event
async def on_member_join(member):
    if member.bot:
        return
    try:
        new = await member.guild.invites()
    except discord.Forbidden:
        return
    old = invite_cache.get(member.guild.id, {})
    inviter = None
    for i in new:
        if i.uses > old.get(i.code, 0):
            inviter = i.inviter
            break
    invite_cache[member.guild.id] = {i.code: i.uses for i in new}
    if inviter and inviter.id != member.id:
        db = load()
        db["invited"][str(member.id)] = str(inviter.id)
        db["invites"][str(inviter.id)] = db["invites"].get(str(inviter.id), 0) + 1
        st = _inv_stat(db, inviter.id)
        st["joins"] += 1
        if (discord.utils.utcnow() - member.created_at).days < 7:
            st["fake"] += 1
        seen = db.setdefault("seen", [])
        if str(member.id) in seen:
            st["rejoins"] += 1
        else:
            seen.append(str(member.id))
        save(db)


@bot.event
async def on_member_remove(member):
    db = load()
    inviter = db["invited"].pop(str(member.id), None)
    if inviter:
        db["invites"][inviter] = max(0, db["invites"].get(inviter, 0) - 1)
        _inv_stat(db, inviter)["left"] += 1
        save(db)


# =====================================================
#                    VPS FLOWS (UI)
# =====================================================
async def deploy_flow(interaction, user, plan, *, free, send_link, vouched):
    """Deploy + progress panel. interaction pehle defer ho chuka hona chahiye."""
    plan = norm_plan(plan)
    v = {"owner": str(user.id), "plan": plan["name"], "node": plan["node"],
         "ram": plan["ram"], "cpus": plan["cpus"], "disk": plan["disk"],
         "vouched": vouched, "created": int(time.time())}
    p = Panel(interaction, "🚀 Deploying VPS Instance", None, v)
    await p.show(5, f"⏳ Allocating resources for **{plan['name']}**...")
    cid, err = await p.run(vm_create(plan), 15, "📦 Allocating root shell (instant, no apt install)...", cap=85, step=6)
    if cid is None:
        return await p.fail(f"Deploy fail:\n```{err}```")
    p.cid = cid
    if VM_NOTES.get(cid):
        v["note"] = VM_NOTES.pop(cid)
    if cid in VM_SSH:
        v.update(VM_SSH.pop(cid))

    db = load()
    if DEDUCT_INVITES and not free:
        uid = str(user.id)
        db["invites"][uid] = 0   # claim ke baad saare invites clear
    db["vps"][cid] = v
    save(db)

    if send_link:
        await p.show(90, "🔐 Generating sshx session...")
        link = await get_sshx_link(cid)
        if link:
            ok = await dm_link(user, link, cid, "🎁 Tumhara VPS ready hai!", node=v["node"])
            note = LOCK_OK if ok else LOCK_FAIL
        else:
            note = "⚠️ sshx link nahi mila - `/regen_sshx` use karo."
            if SSH_ACCESS:
                await dm_ssh(user, cid)
    elif REQUIRE_VOUCH and not vouched:
        note = "👉 Ab `/vouch` karo, phir `/start` use karo."
    else:
        note = "👉 Ab `/start` use karo."
    await p.show(100, "✅ VPS Deployed Successfully!", color=GREEN, done=True, note=note)


async def flow_start(interaction, cid, v):
    await interaction.response.defer()
    p = Panel(interaction, "▶️ Starting VPS Instance Engine", cid, v)
    await p.show(10, "⚙️ Booting container...")
    await vm_start(cid)
    link = await p.run(get_sshx_link(cid), 50, "🔌 Starting sshx session...", cap=95, step=8)
    if not link:
        return await p.fail("sshx link nahi mila. Thodi der baad `/regen_sshx` try karo.")
    ok = await dm_link(interaction.user, link, cid, node=v["node"])
    await p.show(100, "✅ VPS Boot Sequence Completed Successfully!", color=GREEN, done=True,
                 note=LOCK_OK if ok else LOCK_FAIL)


async def flow_stop(interaction, cid, v):
    await interaction.response.defer()
    p = Panel(interaction, "⏹️ Stopping VPS Instance Engine", cid, v)
    await p.show(20, "📡 Sending shutdown signal...")
    await p.run(vm_stop(cid), 50, "🛑 Halting container...", cap=90, step=10)
    await p.show(100, "🛑 VPS Shutdown Sequence Completed Successfully!", color=RED, done=True,
                 note="💤 VPS offline hai. Dobara chalane ke liye `/start` use karo.")


async def flow_restart(interaction, cid, v):
    await interaction.response.defer()
    p = Panel(interaction, "🔄 Restarting VPS Instance Engine", cid, v)
    await p.show(15, "🛑 Stopping container...")
    await vm_restart(cid)
    link = await p.run(get_sshx_link(cid), 50, "🔌 Booting & starting sshx session...", cap=95, step=8)
    if not link:
        return await p.fail("Restart hua par sshx link nahi mila. `/regen_sshx` try karo.")
    ok = await dm_link(interaction.user, link, cid, "♻️ VPS restarted", node=v["node"])
    await p.show(100, "✅ VPS Restart Sequence Completed Successfully!", color=GREEN, done=True,
                 note=LOCK_OK if ok else LOCK_FAIL)


async def flow_regen(interaction, cid, v):
    await interaction.response.defer()
    p = Panel(interaction, "🔁 Regenerating sshx Session", cid, v)
    await p.show(20, "🧹 Closing old sshx session...")
    link = await p.run(get_sshx_link(cid), 40, "🔑 Generating new sshx link...", cap=95, step=8)
    if not link:
        return await p.fail("sshx link nahi mila.")
    ok = await dm_link(interaction.user, link, cid, "🔄 New sshx link", node=v["node"])
    await p.show(100, "✅ New sshx Session Generated Successfully!", color=GREEN, done=True,
                 note=("🔒 Naya link DM me bhej diya. Purana link band ho gaya." if ok else LOCK_FAIL))


async def flow_delete(interaction, cid, v):
    await interaction.response.defer()
    p = Panel(interaction, "🗑️ Deleting VPS Instance", cid, v)
    await p.show(30, "🧹 Stopping & removing container...")
    await p.run(vm_rm(cid), 60, "🗑️ Wiping data...", cap=90, step=10)
    db = load()
    db["vps"].pop(cid, None)
    save(db)
    await p.show(100, "✅ VPS Deleted Successfully!", color=RED, done=True,
                 note="🗑️ Saara data permanently delete ho gaya.")


# =====================================================
#                    /buy PANEL
# =====================================================
class OsSelect(discord.ui.Select):
    def __init__(self, cid, owner_id):
        options = [discord.SelectOption(label=name, value=key, description=img)
                   for key, (name, img) in OS_CHOICES.items()]
        super().__init__(placeholder="Naya OS select karo", options=options)
        self.cid = cid
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Ye panel tumhara nahi hai.", ephemeral=True)
        v = load()["vps"].get(self.cid)
        if not v:
            return await interaction.response.send_message("❌ VPS nahi mila.", ephemeral=True)
        await interaction.response.defer()
        await flow_rebuild(interaction, self.cid, v, self.values[0])


class OsView(discord.ui.View):
    def __init__(self, cid, owner_id):
        super().__init__(timeout=120)
        self.add_item(OsSelect(cid, owner_id))


async def flow_rebuild(interaction, cid, v, os_key):
    name = OS_CHOICES[os_key][0]
    p = Panel(interaction, f"🔄 Rebuilding VPS ({name})", cid, v)
    await p.show(5, f"⏳ **{name}** image taiyaar ho raha hai (pehli baar kuch minute lagte hain)...")
    err = await p.run(_docker_ensure_base_image(os_key), 10, f"📥 {name} image build ho rahi hai...", cap=60, step=4)
    if err:
        return await p.fail(f"Image build fail:\n```{err}```")
    await p.show(65, "🧹 Purana container hata raha hu...")
    await vm_rm(cid)
    plan = {"name": v.get("plan", "Rebuild"), "node": v["node"], "ram": v["ram"], "cpus": v["cpus"],
            "disk": v["disk"], "os": os_key, "cid": cid}
    if v.get("ssh_port"):
        plan["ssh_existing"] = {"ssh_port": v["ssh_port"], "ssh_pass": v["ssh_pass"]}
    new_cid, err = await p.run(vm_create(plan), 70, "📦 Naya container ban raha hai...", cap=90, step=5)
    if new_cid is None:
        return await p.fail(f"Rebuild fail (VPS record bacha hai, `/rebuild` dobara chalao):\n```{err}```")
    VM_SSH.pop(cid, None)
    vnote = VM_NOTES.pop(cid, None)
    db = load()
    if cid in db["vps"]:
        db["vps"][cid]["os"] = os_key
        if vnote:
            db["vps"][cid]["note"] = vnote
        save(db)
        v = db["vps"][cid]
    p.v = v
    await p.show(92, "🔐 sshx session bana raha hu...")
    link = await get_sshx_link(cid)
    if link:
        ok = await dm_link(interaction.user, link, cid, f"🔄 VPS rebuilt ({name})", node=v["node"])
        note = LOCK_OK if ok else LOCK_FAIL
    else:
        note = "⚠️ sshx link nahi mila - `/regen_sshx` use karo."
    await p.show(100, f"✅ Rebuild complete - ab OS **{name}** hai.", color=GREEN, done=True, note=note)


@tree.command(name="rebuild", description="VPS ka OS badlo (saara data delete hoga)")
@app_commands.describe(container_id="Container ID")
@app_commands.autocomplete(container_id=cid_autocomplete)
async def rebuild(interaction: discord.Interaction, container_id: str):
    v = await check_owner(interaction, container_id)
    if v is None:
        return
    if not USE_DOCKER:
        return await interaction.response.send_message("❌ Rebuild sirf Docker mode me chalta hai.", ephemeral=True)
    e = discord.Embed(title="🔄 Rebuild VPS", color=PURPLE,
                      description=f"Container `{container_id}` ke liye naya OS select karo.\n"
                                  f"⚠️ **Is VPS ka saara data delete ho jayega.** RAM/CPU/Disk wahi rahenge.")
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    await interaction.response.send_message(embed=e, view=OsView(container_id, interaction.user.id), ephemeral=True)


class PlanSelect(discord.ui.Select):
    def __init__(self, plans, owner_id, send_link=False):
        options = []
        for k, p in plans.items():
            p = norm_plan(p)
            options.append(discord.SelectOption(
                label=p["name"][:100], value=k,
                description=f"{p['node']} • {p['ram']} RAM • {p['cpus']} CPU • {p['disk']} • {p['invites']} invites"[:100]))
        super().__init__(placeholder="Choose a VPS plan", options=options[:25])
        self.owner_id = owner_id
        self.send_link = send_link

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Ye panel tumhara nahi hai.", ephemeral=True)
        db = load()
        plan = db["plans"].get(self.values[0])
        uid = str(interaction.user.id)
        have = db["invites"].get(uid, 0)
        mine = [c for c, v in db["vps"].items() if v["owner"] == uid]
        admin = is_admin(interaction)

        if plan is None:
            return await interaction.response.send_message("❌ Plan nahi mila.", ephemeral=True)
        plan = norm_plan(plan)
        if len(mine) >= MAX_PER_USER and not admin:
            return await interaction.response.send_message(
                f"❌ Tumhare paas already {len(mine)} VPS hai. Pehle delete karo.", ephemeral=True)
        if have < plan["invites"] and not admin:
            return await interaction.response.send_message(
                f"❌ You need at least {plan['invites']} invites to buy {plan['name']}. "
                f"Tumhare paas {have} hain. Deployment was cancelled.", ephemeral=True)

        await interaction.response.defer()
        await deploy_flow(interaction, interaction.user, plan, free=admin,
                          send_link=self.send_link, vouched=not REQUIRE_VOUCH)


class PlanView(discord.ui.View):
    def __init__(self, plans, owner_id, send_link=False):
        super().__init__(timeout=180)
        self.add_item(PlanSelect(plans, owner_id, send_link))


@tree.command(name="claim", description="Invites se NODE select karke VPS claim karo")
async def claim(interaction: discord.Interaction):
    db = load()
    if not db["plans"]:
        return await interaction.response.send_message("❌ Koi node available nahi.", ephemeral=True)
    have = db["invites"].get(str(interaction.user.id), 0)
    e = discord.Embed(
        title="🎁 Claim Your VPS",
        description=f"📨 Tumhare invites: **{have}**\nNeeche dropdown se NODE select karo. Invites kat jayenge "
                    f"aur sshx link tumhare DM me aayega.",
        color=BLUE)
    for k, p in db["plans"].items():
        p = norm_plan(p)
        ok = "✅ Claim kar sakte ho" if have >= p["invites"] else f"❌ {p['invites'] - have} invites aur chahiye"
        e.add_field(name=f"{p['node']}",
                    value=f"`{p['ram']} RAM` • `{p['cpus']} CPU` • `{p['disk']} Disk` • `{p['invites']} invites`\n{ok}",
                    inline=False)
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    await interaction.response.send_message(embed=e, view=PlanView(db["plans"], interaction.user.id, send_link=True))


@tree.command(name="buy", description="VPS Purchase Panel kholo")
async def buy(interaction: discord.Interaction):
    db = load()
    if not db["plans"]:
        return await interaction.response.send_message("❌ Koi plan available nahi.", ephemeral=True)
    e = discord.Embed(
        title="🛒 VPS Purchase Panel",
        description="Choose a plan from the list below. Each plan checks your invite score before deployment.",
        color=BLUE)
    for k, p in db["plans"].items():
        p = norm_plan(p)
        e.add_field(
            name=f"Plan {k}: {p['name']}",
            value=f"`{p['node']}` • `{p['ram']} RAM` • `{p['cpus']} CPU` • `{p['disk']} Disk` • `{p['invites']} invites required`",
            inline=False)
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    await interaction.response.send_message(embed=e, view=PlanView(db["plans"], interaction.user.id))


# =====================================================
#                    USER COMMANDS
# =====================================================
@tree.command(name="vouch", description="Apne VPS ke liye vouch do")
@app_commands.describe(container_id="Container ID", message="Vouch message")
@app_commands.autocomplete(container_id=cid_autocomplete)
async def vouch(interaction: discord.Interaction, container_id: str, message: str):
    v = await check_owner(interaction, container_id)
    if v is None:
        return
    if len(message) < 5:
        return await interaction.response.send_message("❌ Vouch message thoda lamba likho.", ephemeral=True)
    db = load()
    db["vps"][container_id]["vouched"] = True
    save(db)
    e = discord.Embed(
        description=f"✅ {interaction.user.mention} vouch recorded for 1 VPS instance(s). "
                    f"You can now use `/start {container_id}`.", color=GREEN)
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    await interaction.response.send_message(embed=e)


@tree.command(name="start", description="VPS start karo")
@app_commands.describe(container_id="Container ID")
@app_commands.autocomplete(container_id=cid_autocomplete)
async def start(interaction: discord.Interaction, container_id: str):
    v = await check_owner(interaction, container_id)
    if v is None:
        return
    if REQUIRE_VOUCH and not v.get("vouched"):
        return await interaction.response.send_message(f"❌ Pehle `/vouch {container_id}` karo.", ephemeral=True)
    await flow_start(interaction, container_id, v)


@tree.command(name="regen_sshx", description="Naya sshx link generate karo")
@app_commands.describe(container_id="Container ID")
@app_commands.autocomplete(container_id=cid_autocomplete)
async def regen_sshx(interaction: discord.Interaction, container_id: str):
    v = await check_owner(interaction, container_id)
    if v is not None:
        await flow_regen(interaction, container_id, v)


@tree.command(name="ssh_info", description="SSH IP, port aur password DM me lo")
@app_commands.describe(container_id="Container ID")
@app_commands.autocomplete(container_id=cid_autocomplete)
async def ssh_info(interaction: discord.Interaction, container_id: str):
    v = await check_owner(interaction, container_id)
    if v is None:
        return
    if not v.get("ssh_port"):
        return await interaction.response.send_message(
            "❌ Is VPS me SSH access nahi hai (admin ne `ssh_access` ON karke deploy nahi kiya).", ephemeral=True)
    ok = await dm_ssh(interaction.user, container_id)
    await interaction.response.send_message("🔒 SSH details DM me bhej di." if ok else LOCK_FAIL, ephemeral=True)


@tree.command(name="stop", description="VPS stop karo")
@app_commands.autocomplete(container_id=cid_autocomplete)
async def stop(interaction: discord.Interaction, container_id: str):
    v = await check_owner(interaction, container_id)
    if v is not None:
        await flow_stop(interaction, container_id, v)


@tree.command(name="restart", description="VPS restart karo aur naya sshx link lo")
@app_commands.autocomplete(container_id=cid_autocomplete)
async def restart(interaction: discord.Interaction, container_id: str):
    v = await check_owner(interaction, container_id)
    if v is not None:
        await flow_restart(interaction, container_id, v)


@tree.command(name="delete", description="VPS permanently delete karo")
@app_commands.autocomplete(container_id=cid_autocomplete)
async def delete(interaction: discord.Interaction, container_id: str):
    v = await check_owner(interaction, container_id)
    if v is not None:
        await flow_delete(interaction, container_id, v)


@tree.command(name="status", description="VPS ka status dekho")
@app_commands.autocomplete(container_id=cid_autocomplete)
async def status(interaction: discord.Interaction, container_id: str):
    v = await check_owner(interaction, container_id)
    if v is None:
        return
    await interaction.response.defer(ephemeral=True)
    running = await vm_running(container_id)
    e = discord.Embed(title="📊 VPS Status", color=GREEN if running else RED)
    e.add_field(name="📦 Container ID", value=f"`{container_id}`")
    e.add_field(name="🌐 Cluster Node", value=f"`{v['node']}`")
    e.add_field(name="🧩 Plan", value=v["plan"])
    e.add_field(name="⚡ State", value="🟢 Running" if running else "🔴 Stopped")
    r = res_str(v)
    if r:
        e.add_field(name="💾 Resources", value=r, inline=False)
    if running:
        e.add_field(name="📈 Usage", value=await vm_stats(container_id), inline=False)
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    await interaction.followup.send(embed=e, ephemeral=True)


@tree.command(name="list", description="Tumhare VPS ki list")
async def list_vps(interaction: discord.Interaction):
    db = load()
    mine = {c: v for c, v in db["vps"].items() if v["owner"] == str(interaction.user.id)}
    if not mine:
        return await interaction.response.send_message("Koi VPS nahi hai. `/buy` use karo.", ephemeral=True)
    e = discord.Embed(title="📋 Your VPS Instances", color=BLUE)
    for c, v in mine.items():
        e.add_field(name=f"{v['plan']} • {v['node']}",
                    value=f"`{c}`\n{res_str(v) or ''}", inline=False)
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    await interaction.response.send_message(embed=e, ephemeral=True)


@tree.command(name="invites", description="Apne ya kisi ke invites dekho")
async def invites(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    n = load()["invites"].get(str(user.id), 0)
    await interaction.response.send_message(f"📨 {user.mention} ke **{n}** invites hain.", ephemeral=True)


@tree.command(name="i", description="Apne invites dekho")
async def i_cmd(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    n = load()["invites"].get(str(user.id), 0)
    await interaction.response.send_message(f"📨 {user.mention} ke **{n}** invites hain.", ephemeral=True)


@tree.command(name="plans", description="Available plans dekho")
async def plans(interaction: discord.Interaction):
    db = load()
    e = discord.Embed(title="📦 Available Plans", color=BLUE)
    for k, p in db["plans"].items():
        p = norm_plan(p)
        e.add_field(name=f"Plan {k}: {p['name']}",
                    value=f"`{p['node']}` • `{p['ram']} RAM` • `{p['cpus']} CPU` • `{p['disk']} Disk` • `{p['invites']} invites`",
                    inline=False)
    if not db["plans"]:
        e.description = "Koi plan nahi."
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    await interaction.response.send_message(embed=e, ephemeral=True)


# =====================================================
#                    ADMIN COMMANDS
# =====================================================
@tree.command(name="admin_deploy", description="[Admin] Custom RAM/CPU/Disk ke saath user ke liye VPS deploy karo")
@app_commands.describe(
    ram="RAM, jaise 512m, 2g, 8g",
    cpu="CPU cores, jaise 0.5, 1, 4",
    disk="Disk, jaise 10g, 100g",
    user="Jis user ko VPS dena hai",
    node="Node ka label (optional), jaise NODE1",
    send_link="Deploy ke baad sshx link turant user ko DM karo",
    ssh_access="True = SSH IP/port/password bhi DM me do (Termius). False = sirf sshx")
@admin_only()
async def admin_deploy(interaction: discord.Interaction, ram: str, cpu: str, disk: str,
                       user: discord.Member, node: str = None, send_link: bool = True,
                       ssh_access: bool = False):
    plan, err = resolve_plan(load(), None, ram, cpu, disk, node)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    plan["ssh"] = bool(ssh_access)
    await interaction.response.defer()
    await deploy_flow(interaction, user, plan, free=True, send_link=send_link, vouched=True)


@tree.command(name="admin_start", description="[Admin] Kisi bhi VPS ko start karo")
@app_commands.autocomplete(container_id=cid_autocomplete)
@admin_only()
async def admin_start(interaction: discord.Interaction, container_id: str):
    v = load()["vps"].get(container_id)
    if not v:
        return await interaction.response.send_message("❌ Container nahi mila.", ephemeral=True)
    await flow_start(interaction, container_id, v)


@tree.command(name="admin_stop", description="[Admin] Kisi bhi VPS ko stop karo")
@app_commands.autocomplete(container_id=cid_autocomplete)
@admin_only()
async def admin_stop(interaction: discord.Interaction, container_id: str):
    v = load()["vps"].get(container_id)
    if not v:
        return await interaction.response.send_message("❌ Container nahi mila.", ephemeral=True)
    await flow_stop(interaction, container_id, v)


@tree.command(name="admin_regen_sshx", description="[Admin] Kisi bhi VPS ka naya sshx link lo (tumhe DM aayega)")
@app_commands.autocomplete(container_id=cid_autocomplete)
@admin_only()
async def admin_regen_sshx(interaction: discord.Interaction, container_id: str):
    v = load()["vps"].get(container_id)
    if not v:
        return await interaction.response.send_message("❌ Container nahi mila.", ephemeral=True)
    await flow_regen(interaction, container_id, v)


@tree.command(name="admin_delete", description="[Admin] Kisi ka bhi VPS delete karo")
@app_commands.autocomplete(container_id=cid_autocomplete)
@admin_only()
async def admin_delete(interaction: discord.Interaction, container_id: str):
    v = load()["vps"].get(container_id)
    if not v:
        return await interaction.response.send_message("❌ Container nahi mila.", ephemeral=True)
    await flow_delete(interaction, container_id, v)


@tree.command(name="admin_list", description="[Admin] Saare VPS dekho")
@admin_only()
async def admin_list(interaction: discord.Interaction):
    db = load()
    if not db["vps"]:
        return await interaction.response.send_message("Koi VPS nahi hai.", ephemeral=True)
    lines = [f"• `{c}` — <@{v['owner']}> — {v['plan']} ({v.get('ram', '?')}/{v.get('cpus', '?')}cpu/{v.get('disk', '?')})"
             for c, v in db["vps"].items()]
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@tree.command(name="add_invites", description="[Admin] User ko invites do")
@admin_only()
async def add_invites(interaction: discord.Interaction, user: discord.Member, amount: int):
    db = load()
    k = str(user.id)
    db["invites"][k] = max(0, db["invites"].get(k, 0) + amount)
    save(db)
    await interaction.response.send_message(f"✅ {user.mention} ke ab {db['invites'][k]} invites hain.", ephemeral=True)


@tree.command(name="remove_invites", description="[Admin] User ke invites kam karo")
@admin_only()
async def remove_invites(interaction: discord.Interaction, user: discord.Member, amount: int):
    db = load()
    k = str(user.id)
    db["invites"][k] = max(0, db["invites"].get(k, 0) - amount)
    save(db)
    await interaction.response.send_message(f"✅ {user.mention} ke ab {db['invites'][k]} invites hain.", ephemeral=True)


@tree.command(name="add_plan", description="[Admin] Naya plan add karo")
@app_commands.describe(name="Plan name", node="Node label, jaise NODE1", ram="jaise 2g ya 512m",
                       cpus="jaise 1 ya 0.5", disk="jaise 20g", invites="Required invites")
@admin_only()
async def add_plan(interaction: discord.Interaction, name: str, node: str, ram: str,
                   cpus: str, disk: str, invites: int):
    try:
        plan = {"name": name, "node": node.strip().upper(), "ram": parse_size(ram, "RAM"),
                "cpus": parse_cpus(cpus), "disk": parse_size(disk, "Disk", units="mgt"),
                "invites": max(0, invites)}
    except ValueError as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
    db = load()
    key = str(max([int(k) for k in db["plans"]] + [0]) + 1)
    db["plans"][key] = plan
    save(db)
    await interaction.response.send_message(f"✅ Plan {key} add ho gaya: {name}", ephemeral=True)


@tree.command(name="remove_plan", description="[Admin] Plan hatao")
@app_commands.autocomplete(plan_number=plan_autocomplete)
@admin_only()
async def remove_plan(interaction: discord.Interaction, plan_number: str):
    db = load()
    if db["plans"].pop(plan_number, None) is None:
        return await interaction.response.send_message("❌ Plan nahi mila.", ephemeral=True)
    save(db)
    await interaction.response.send_message(f"🗑️ Plan {plan_number} hata diya.", ephemeral=True)


# =====================================================
#                    OWNER COMMANDS
# =====================================================
@tree.command(name="add_admin", description="[Owner] Kisi ko bot admin banao")
@owner_only()
async def add_admin(interaction: discord.Interaction, user: discord.Member):
    db = load()
    if str(user.id) in db["admins"]:
        return await interaction.response.send_message("Ye already admin hai.", ephemeral=True)
    db["admins"].append(str(user.id))
    save(db)
    await interaction.response.send_message(f"✅ {user.mention} ab bot admin hai.", ephemeral=True)


@tree.command(name="remove_admin", description="[Owner] Bot admin hatao")
@owner_only()
async def remove_admin(interaction: discord.Interaction, user: discord.Member):
    db = load()
    if str(user.id) not in db["admins"]:
        return await interaction.response.send_message("Ye admin nahi hai.", ephemeral=True)
    db["admins"].remove(str(user.id))
    save(db)
    await interaction.response.send_message(f"✅ {user.mention} ab admin nahi raha.", ephemeral=True)


@tree.command(name="list_admins", description="[Owner] Saare bot admins dekho")
@owner_only()
async def list_admins(interaction: discord.Interaction):
    db = load()
    owners = "\n".join(f"👑 <@{i}>" for i in OWNER_IDS) or "—"
    admins = "\n".join(f"🛡️ <@{i}>" for i in db["admins"]) or "Koi extra admin nahi."
    await interaction.response.send_message(f"**Owners**\n{owners}\n\n**Admins**\n{admins}", ephemeral=True)


@tree.command(name="owner_stats", description="[Owner] Bot ke overall stats")
@owner_only()
async def owner_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    db = load()
    running_n = await vm_running_count(list(db["vps"].keys()))
    users = len({v["owner"] for v in db["vps"].values()})
    e = discord.Embed(title="📈 Owner Stats", color=PURPLE)
    e.add_field(name="Total VPS", value=str(len(db["vps"])))
    e.add_field(name="Running", value=str(running_n))
    e.add_field(name="Users with VPS", value=str(users))
    e.add_field(name="Plans", value=str(len(db["plans"])))
    e.add_field(name="Admins", value=str(len(db["admins"])))
    e.add_field(name="Backend", value="Docker" if USE_DOCKER else "Local")
    e.set_footer(text=f"{BOT_NAME} • VPS Manager")
    await interaction.followup.send(embed=e, ephemeral=True)


@tree.command(name="delete_user_vps", description="[Owner] Kisi user ke saare VPS delete karo")
@owner_only()
async def delete_user_vps(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    db = load()
    ids = [c for c, v in db["vps"].items() if v["owner"] == str(user.id)]
    for c in ids:
        await vm_rm(c)
        db["vps"].pop(c, None)
    save(db)
    await interaction.followup.send(f"🗑️ {user.mention} ke {len(ids)} VPS delete ho gaye.", ephemeral=True)


@tree.command(name="stop_all", description="[Owner] Saare VPS stop karo")
@owner_only()
async def stop_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ids = list(load()["vps"].keys())
    for c in ids:
        await vm_stop(c)
    await interaction.followup.send(f"🛑 {len(ids)} VPS stop ho gaye.", ephemeral=True)


@tree.command(name="reset_invites", description="[Owner] User ke invites 0 karo")
@owner_only()
async def reset_invites(interaction: discord.Interaction, user: discord.Member):
    db = load()
    db["invites"][str(user.id)] = 0
    save(db)
    await interaction.response.send_message(f"✅ {user.mention} ke invites reset ho gaye.", ephemeral=True)


@tree.error
async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "❌ Ye command tumhare liye nahi hai."
    else:
        msg = f"❌ Error: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if not TOKEN or TOKEN == "YOUR_BOT_TOKEN":
    sys.exit("❌ DISCORD_TOKEN set nahi hai. Bot folder me '.env' file banao: DISCORD_TOKEN=tumhara_token")
if not OWNER_IDS:
    print("⚠️ OWNER_IDS khaali hai - owner commands kisi ke liye nahi chalenge.")
if USE_DOCKER:
    print("🐳 Backend: Docker")
else:
    print("🖥️ Backend: LOCAL (Docker nahi mila). Har VPS ek alag folder + sshx session hai, isolation aur limits nahi hain.")

bot.run(TOKEN.strip())
