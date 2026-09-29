import discord
from discord.ext import commands
import asyncio
import subprocess
import json
from datetime import datetime
import shlex
import logging
import shutil
import os
from typing import Optional, List, Dict, Any
import threading
import time
import sqlite3
import random
import requests
import secrets
import string
import re

# Load environment variables
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
BOT_NAME = os.getenv('BOT_NAME', 'DB-v1')
PREFIX = os.getenv('PREFIX', '!')
YOUR_SERVER_IP = os.getenv('YOUR_SERVER_IP', '127.0.0.1')

# ---- Public IP detection (used for SSH login info shown to users) ----
_cached_public_ip = None

def get_public_ip() -> str:
    """Fetch the server's public IP via ifconfig.me, caching the result.
    Falls back to YOUR_SERVER_IP env var if the lookup fails."""
    global _cached_public_ip
    if _cached_public_ip:
        return _cached_public_ip
    try:
        resp = requests.get("https://ifconfig.me/ip", timeout=5)
        ip = resp.text.strip()
        if ip:
            _cached_public_ip = ip
            return ip
    except Exception as e:
        logger.warning(f"Failed to fetch public IP from ifconfig.me: {e}")
    return YOUR_SERVER_IP

_raw_main_admin_ids = os.getenv('MAIN_ADMIN_ID', '1405866008127864852')
MAIN_ADMIN_IDS_ENV = [uid.strip() for uid in _raw_main_admin_ids.split(',') if uid.strip()]
MAIN_ADMIN_ID = int(MAIN_ADMIN_IDS_ENV[0])  # kept for backward-compat display purposes
VPS_USER_ROLE_ID = int(os.getenv('VPS_USER_ROLE_ID', '1210291131301101618'))
DEFAULT_STORAGE_POOL = os.getenv('DEFAULT_STORAGE_POOL', 'default')
BOT_VERSION = os.getenv('BOT_VERSION', '9.0-PRO')
BOT_DEVELOPER = os.getenv('BOT_DEVELOPER', 'EVILSAAD')

# OS Options for VPS Creation and Reinstall
OS_OPTIONS = [
    {"label": "Ubuntu 20.04 LTS", "value": "ubuntu:20.04"},
    {"label": "Ubuntu 22.04 LTS", "value": "ubuntu:22.04"},
    {"label": "Ubuntu 24.04 LTS", "value": "ubuntu:24.04"},
    {"label": "Debian 10 (Buster)", "value": "images:debian/10"},
    {"label": "Debian 11 (Bullseye)", "value": "images:debian/11"},
    {"label": "Debian 12 (Bookworm)", "value": "images:debian/12"},
    {"label": "Debian 13 (Trixie)", "value": "images:debian/13"},
]

# Configure logging to file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(f'{BOT_NAME.lower()}_vps_bot')

# Database setup
def get_db():
    conn = sqlite3.connect('vps.db')
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS main_admins (
        user_id TEXT PRIMARY KEY
    )''')
    for uid in MAIN_ADMIN_IDS_ENV:
        cur.execute('INSERT OR IGNORE INTO main_admins (user_id) VALUES (?)', (uid,))
    cur.execute('''CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        location TEXT,
        total_vps INTEGER,
        tags TEXT DEFAULT '[]',
        api_key TEXT,
        url TEXT,
        is_local INTEGER DEFAULT 0
    )''')
    cur.execute('SELECT COUNT(*) FROM nodes WHERE is_local = 1')
    if cur.fetchone()[0] == 0:
        cur.execute('INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    ('Node 1', 'Local', 100, '[]', None, None, 1))
    cur.execute('''CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        node_id INTEGER NOT NULL DEFAULT 1,
        container_name TEXT UNIQUE NOT NULL,
        ram TEXT NOT NULL,
        cpu TEXT NOT NULL,
        storage TEXT NOT NULL,
        config TEXT NOT NULL,
        os_version TEXT DEFAULT 'ubuntu:22.04',
        status TEXT DEFAULT 'stopped',
        suspended INTEGER DEFAULT 0,
        whitelisted INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        shared_with TEXT DEFAULT '[]',
        suspension_history TEXT DEFAULT '[]',
        root_password TEXT DEFAULT ''
    )''')
    cur.execute('PRAGMA table_info(vps)')
    info = cur.fetchall()
    columns = [col[1] for col in info]
    if 'os_version' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN os_version TEXT DEFAULT 'ubuntu:22.04'")
    if 'node_id' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN node_id INTEGER DEFAULT 1")
    if 'root_password' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN root_password TEXT DEFAULT ''")
    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')
    settings_init = [
        ('cpu_threshold', '90'),
        ('ram_threshold', '90'),
    ]
    for key, value in settings_init:
        cur.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
    cur.execute('''CREATE TABLE IF NOT EXISTS port_allocations (
        user_id TEXT PRIMARY KEY,
        allocated_ports INTEGER DEFAULT 0
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS port_forwards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        vps_container TEXT NOT NULL,
        vps_port INTEGER NOT NULL,
        host_port INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()

def get_setting(key: str, default: Any = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def get_nodes() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM nodes')
    rows = cur.fetchall()
    conn.close()
    nodes = [dict(row) for row in rows]
    for node in nodes:
        node['tags'] = json.loads(node['tags'])
    return nodes

def get_node(node_id: int) -> Optional[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM nodes WHERE id = ?', (node_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        node = dict(row)
        node['tags'] = json.loads(node['tags'])
        return node
    return None

def get_current_vps_count(node_id: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM vps WHERE node_id = ?', (node_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count

def get_vps_data() -> Dict[str, List[Dict[str, Any]]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM vps')
    rows = cur.fetchall()
    conn.close()
    data = {}
    for row in rows:
        user_id = row['user_id']
        if user_id not in data:
            data[user_id] = []
        vps = dict(row)
        vps['shared_with'] = json.loads(vps['shared_with'])
        vps['suspension_history'] = json.loads(vps['suspension_history'])
        vps['suspended'] = bool(vps['suspended'])
        vps['whitelisted'] = bool(vps['whitelisted'])
        vps['os_version'] = vps.get('os_version', 'ubuntu:22.04')
        data[user_id].append(vps)
    return data

def get_admins() -> List[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM admins')
    rows = cur.fetchall()
    conn.close()
    return [row['user_id'] for row in rows]

def get_main_admins() -> List[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM main_admins')
    rows = cur.fetchall()
    conn.close()
    ids = [row['user_id'] for row in rows]
    return ids if ids else [str(MAIN_ADMIN_ID)]

def save_main_admins():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM main_admins')
    for uid in main_admin_ids:
        cur.execute('INSERT INTO main_admins (user_id) VALUES (?)', (uid,))
    conn.commit()
    conn.close()

def save_vps_data():
    conn = get_db()
    cur = conn.cursor()
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            shared_json = json.dumps(vps['shared_with'])
            history_json = json.dumps(vps['suspension_history'])
            suspended_int = 1 if vps['suspended'] else 0
            whitelisted_int = 1 if vps.get('whitelisted', False) else 0
            os_ver = vps.get('os_version', 'ubuntu:22.04')
            created_at = vps.get('created_at', datetime.now().isoformat())
            node_id = vps.get('node_id', 1)
            root_password = vps.get('root_password', '')
            if 'id' not in vps or vps['id'] is None:
                cur.execute('''INSERT INTO vps (user_id, node_id, container_name, ram, cpu, storage, config, os_version, status, suspended, whitelisted, created_at, shared_with, suspension_history, root_password)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'], vps['storage'], vps['config'],
                             os_ver, vps['status'], suspended_int, whitelisted_int,
                             created_at, shared_json, history_json, root_password))
                vps['id'] = cur.lastrowid
            else:
                cur.execute('''UPDATE vps SET user_id = ?, node_id = ?, container_name = ?, ram = ?, cpu = ?, storage = ?, config = ?, os_version = ?, status = ?, suspended = ?, whitelisted = ?, shared_with = ?, suspension_history = ?, root_password = ?
                               WHERE id = ?''',
                            (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'], vps['storage'], vps['config'],
                             os_ver, vps['status'], suspended_int, whitelisted_int, shared_json, history_json, root_password, vps['id']))
    conn.commit()
    conn.close()

def delete_vps_from_db(container_name: str):
    """Deletes the VPS record and its port forwards from SQLite."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM vps WHERE container_name = ?', (container_name,))
    cur.execute('DELETE FROM port_forwards WHERE vps_container = ?', (container_name,))
    conn.commit()
    conn.close()

def find_node_id_for_container(container_name: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT node_id FROM vps WHERE container_name = ?', (container_name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 1

init_db()

vps_data = get_vps_data()
admin_data = {'admins': get_admins()}
main_admin_ids = set(get_main_admins())

CPU_THRESHOLD = int(get_setting('cpu_threshold', 90))
RAM_THRESHOLD = int(get_setting('ram_threshold', 90))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

resource_monitor_active = True

def truncate_text(text, max_length=1024):
    if not text:
        return text
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

def create_embed(title, description="", color=0x1a1a1a):
    embed = discord.Embed(
        title=truncate_text(f"🚀 {BOT_NAME} - {title}", 256),
        description=truncate_text(description, 4096),
        color=color
    )
    minecloud_img_url = "https://media.discordapp.net/attachments/1554370796506320917/1554370864517091328/ChatGPT_Image_Jul_4_2026_11_08_41_AM.png?backend=b2&ex=6abca42c&is=6abb52ac&hm=9596d584cc9ec064d73f50ea260518aea799a0be5885244bc925c0684004abdc&=&format=webp&quality=lossless&width=640&height=640"
    embed.set_thumbnail(url=minecloud_img_url)
    embed.set_footer(text=f"{BOT_NAME} VPS Manager v{BOT_VERSION} • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", icon_url=minecloud_img_url)
    return embed

def add_field(embed, name, value, inline=False):
    embed.add_field(
        name=truncate_text(f"▸ {name}", 256),
        value=truncate_text(value, 1024),
        inline=inline
    )
    return embed

def create_success_embed(title, description=""):
    return create_embed(title, description, color=0x00ff88)

def create_error_embed(title, description=""):
    return create_embed(title, description, color=0xff3366)

def create_info_embed(title, description=""):
    return create_embed(title, description, color=0x00ccff)

def is_admin():
    async def predicate(ctx):
        user_id = str(ctx.author.id)
        if user_id in main_admin_ids or user_id in admin_data.get("admins", []):
            return True
        raise commands.CheckFailure("You need admin permissions to use this command. Contact support.")
    return commands.check(predicate)

async def execute_lxc(container_name: str, command: str, timeout=120, node_id: Optional[int] = None):
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    
    if not node:
        raise Exception(f"Node {node_id} not found")
    
    full_command = f"lxc {command}"
    
    if node['is_local']:
        try:
            cmd = shlex.split(full_command)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise asyncio.TimeoutError(f"Command timed out after {timeout} seconds")
            
            if proc.returncode != 0:
                error = stderr.decode().strip() if stderr else "Command failed with no error output"
                raise Exception(f"Local LXC command failed: {error}\nCommand: {full_command}")
            return stdout.decode().strip() if stdout else True
        except Exception as e:
            logger.error(f"LXC Error: {full_command} - {str(e)}")
            raise
    else:
        url = f"{node['url']}/api/execute"
        data = {"command": full_command}
        params = {"api_key": node["api_key"]}
        try:
            response = requests.post(url, json=data, params=params, timeout=timeout)
            response.raise_for_status()
            res = response.json()
            if res.get("returncode", 1) != 0:
                stderr = res.get("stderr", "Command failed")
                raise Exception(f"Remote LXC command failed on {node['name']}: {stderr}")
            return res.get("stdout", True)
        except requests.exceptions.RequestException as e:
            raise Exception(f"Remote execution failed on {node['name']}: {str(e)}")

async def safe_start_container(container_name: str, node_id: int):
    try:
        await execute_lxc(container_name, f"start {container_name}", node_id=node_id)
    except Exception as e:
        err_text = str(e).lower()
        if "already running" in err_text or "is running" in err_text:
            logger.info(f"{container_name} was already running.")
        else:
            raise

async def apply_lxc_config(container_name: str, node_id: int):
    try:
        await execute_lxc(container_name, f"config set {container_name} security.nesting true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.privileged true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.syscalls.intercept.mknod true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.syscalls.intercept.setxattr true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} linux.kernel_modules overlay,loop,nf_nat,ip_tables,ip6_tables,netlink_diag,br_netfilter", node_id=node_id)
        try:
            await execute_lxc(container_name, f"config device add {container_name} fuse unix-char path=/dev/fuse", node_id=node_id)
        except:
            pass
        raw_lxc_config = (
            "lxc.apparmor.profile = unconfined\n"
            "lxc.apparmor.allow_nesting = 1\n"
            "lxc.apparmor.allow_incomplete = 1\n"
            "\n"
            "lxc.cap.drop =\n"
            "lxc.cgroup.devices.allow = a\n"
            "lxc.cgroup2.devices.allow = a\n"
            "\n"
            "lxc.mount.auto = proc:rw sys:rw cgroup:rw shmounts:rw\n"
            "\n"
            "lxc.mount.entry = /dev/fuse dev/fuse none bind,create=file 0 0\n"
        )
        await execute_lxc(container_name, f"config set {container_name} raw.lxc '{raw_lxc_config}'", node_id=node_id)
    except Exception as e:
        logger.error(f"Failed to apply LXC config to {container_name}: {e}")

async def apply_internal_permissions(container_name: str, node_id: int):
    try:
        await asyncio.sleep(5)
        commands = [
            "mkdir -p /etc/sysctl.d/",
            "echo 'net.ipv4.ip_unprivileged_port_start=0' > /etc/sysctl.d/99-custom.conf",
            "echo 'net.ipv4.ping_group_range=0 2147483647' >> /etc/sysctl.d/99-custom.conf",
            "sysctl -p /etc/sysctl.d/99-custom.conf || true"
        ]
        for cmd in commands:
            try:
                await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{cmd}\"", node_id=node_id)
            except Exception as cmd_error:
                pass
    except Exception as e:
        logger.error(f"Internal permission error on {container_name}: {e}")

def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

async def setup_ssh_access(container_name: str, node_id: int) -> str:
    """Fixed & Improved SSH Configurator"""
    password = generate_password()
    commands = [
        "apt-get update -y",
        "apt-get install -y openssh-server openssh-client",
        f"echo 'root:{password}' | chpasswd",
        "sed -i 's/#*PermitRootLogin.*/PermitRootLogin yes/g' /etc/ssh/sshd_config",
        "sed -i 's/#*PasswordAuthentication.*/PasswordAuthentication yes/g' /etc/ssh/sshd_config",
        "mkdir -p /var/run/sshd /run/sshd",
        "service ssh restart || systemctl restart ssh || /usr/sbin/sshd"
    ]
    for cmd in commands:
        try:
            await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{cmd}\"", node_id=node_id, timeout=120)
        except Exception as cmd_error:
            logger.warning(f"SSH setup command log: {cmd} -> {cmd_error}")
    return password

PINGGY_LOG_PATH = "/root/.pinggy_tunnel.log"

def parse_pinggy_address(log_text: str) -> Optional[str]:
    if not log_text:
        return None
    match = re.search(r'tcp://([\w\.\-]+):(\d+)', log_text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return None

async def establish_pinggy_tunnel(container_name: str, node_id: int, retries: int = 5, wait_seconds: int = 4) -> Optional[str]:
    try:
        await execute_lxc(
            container_name,
            f"exec {container_name} -- bash -c \"pkill -f 'free.pinggy.io' >/dev/null 2>&1; rm -f {PINGGY_LOG_PATH}\"",
            node_id=node_id
        )
    except Exception:
        pass

    tunnel_cmd = (
        "setsid nohup ssh -p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        "-o ServerAliveInterval=30 -R0:localhost:22 qr+tcp@free.pinggy.io "
        f"> {PINGGY_LOG_PATH} 2>&1 < /dev/null & disown"
    )
    try:
        await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{tunnel_cmd}\"", node_id=node_id)
    except Exception as e:
        logger.error(f"Pinggy start failed on {container_name}: {e}")
        return None

    for _ in range(retries):
        await asyncio.sleep(wait_seconds)
        try:
            log_output = await execute_lxc(container_name, f"exec {container_name} -- cat {PINGGY_LOG_PATH}", node_id=node_id)
        except Exception:
            log_output = ""
        address = parse_pinggy_address(log_output if isinstance(log_output, str) else "")
        if address:
            return address
    return None

async def get_container_stats(container_name: str, node_id: Optional[int] = None) -> Dict:
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    if not node or node['is_local']:
        status = "running"
        cpu = 0.0
        ram = {'used': 128, 'total': 1024, 'pct': 12.5}
        disk = "1.2G/10G (12%)"
        uptime = "up 1 hour"
        return {"status": status, "cpu": cpu, "ram": ram, "disk": disk, "uptime": uptime}
    return {"status": "unknown", "cpu": 0.0, "ram": {"used": 0, "total": 0, "pct": 0.0}, "disk": "Unknown", "uptime": "Unknown"}

@bot.event
async def on_ready():
    logger.info(f'{bot.user} is connected!')

# Node selection view
class NodeSelectView(discord.ui.View):
    def __init__(self, ram: int, cpu: int, disk: int, user: discord.Member, ctx):
        super().__init__(timeout=300)
        self.ram = ram
        self.cpu = cpu
        self.disk = disk
        self.user = user
        self.ctx = ctx
        nodes = get_nodes()
        options = [discord.SelectOption(label=n['name'], value=str(n['id'])) for n in nodes]
        self.select = discord.ui.Select(placeholder="Select Node", options=options)
        self.select.callback = self.select_node
        self.add_item(self.select)

    async def select_node(self, interaction: discord.Interaction):
        node_id = int(self.select.values[0])
        os_view = OSSelectView(self.ram, self.cpu, self.disk, self.user, self.ctx, node_id)
        await interaction.response.send_message(embed=create_info_embed("Select OS", "Choose the OS for the VPS."), view=os_view, ephemeral=True)

class OSSelectView(discord.ui.View):
    def __init__(self, ram: int, cpu: int, disk: int, user: discord.Member, ctx, node_id: int):
        super().__init__(timeout=300)
        self.ram = ram
        self.cpu = cpu
        self.disk = disk
        self.user = user
        self.ctx = ctx
        self.node_id = node_id
        self.select = discord.ui.Select(placeholder="Select OS", options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS])
        self.select.callback = self.select_os
        self.add_item(self.select)

    async def select_os(self, interaction: discord.Interaction):
        os_version = self.select.values[0]
        await interaction.response.send_message(embed=create_info_embed("Deploying", f"Deploying `{os_version}`... Please wait."), ephemeral=True)
        user_id = str(self.user.id)
        if user_id not in vps_data:
            vps_data[user_id] = []
        
        vps_count = len(vps_data[user_id]) + 1
        container_name = f"{BOT_NAME.lower()}-vps-{user_id}-{vps_count}"
        ram_mb = self.ram * 1024
        try:
            await execute_lxc(container_name, f"init {os_version} {container_name} -s {DEFAULT_STORAGE_POOL}", node_id=self.node_id)
            await execute_lxc(container_name, f"config set {container_name} limits.memory {ram_mb}MB", node_id=self.node_id)
            await execute_lxc(container_name, f"config set {container_name} limits.cpu {self.cpu}", node_id=self.node_id)
            await safe_start_container(container_name, self.node_id)
            await apply_lxc_config(container_name, self.node_id)
            await apply_internal_permissions(container_name, self.node_id)
            root_password = await setup_ssh_access(container_name, self.node_id)
            pinggy_address = await establish_pinggy_tunnel(container_name, self.node_id)

            config_str = f"{self.ram}GB RAM / {self.cpu} CPU / {self.disk}GB Disk"
            vps_info = {
                "container_name": container_name,
                "node_id": self.node_id,
                "ram": f"{self.ram}GB",
                "cpu": str(self.cpu),
                "storage": f"{self.disk}GB",
                "config": config_str,
                "os_version": os_version,
                "status": "running",
                "suspended": False,
                "whitelisted": False,
                "suspension_history": [],
                "created_at": datetime.now().isoformat(),
                "shared_with": [],
                "root_password": root_password,
                "pinggy_address": pinggy_address,
                "id": None
            }
            vps_data[user_id].append(vps_info)
            save_vps_data()
            await interaction.followup.send(embed=create_success_embed("VPS Created", f"Container `{container_name}` active!"), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=create_error_embed("Failed", str(e)), ephemeral=True)

# Delete Confirmation Modal/View
class DeleteConfirmView(discord.ui.View):
    def __init__(self, owner_id, container_name, node_id, actual_idx, parent_manage_view):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.container_name = container_name
        self.node_id = node_id
        self.actual_idx = actual_idx
        self.parent_manage_view = parent_manage_view

    @discord.ui.button(label="⚠️ Yes, Delete VPS", style=discord.ButtonStyle.danger)
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            # 1. Stop LXC container
            try:
                await execute_lxc(self.container_name, f"stop {self.container_name} --force", timeout=60, node_id=self.node_id)
            except Exception:
                pass

            # 2. Delete LXC container completely
            await execute_lxc(self.container_name, f"delete {self.container_name} --force", timeout=60, node_id=self.node_id)

            # 3. Clear database record
            delete_vps_from_db(self.container_name)

            # 4. Clear in-memory vps_data
            if self.owner_id in vps_data:
                vps_data[self.owner_id] = [v for v in vps_data[self.owner_id] if v['container_name'] != self.container_name]

            await interaction.followup.send(
                embed=create_success_embed("Deleted Successfully", f"Container `{self.container_name}` has been completely destroyed and removed."),
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                embed=create_error_embed("Deletion Failed", f"Could not delete container: {e}"),
                ephemeral=True
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Deletion cancelled.", ephemeral=True)

# Updated ManageView with DELETE Button
class ManageView(discord.ui.View):
    def __init__(self, user_id, vps_list, is_shared=False, owner_id=None, is_admin=False, actual_index: Optional[int] = None):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.vps_list = vps_list[:]
        self.selected_index = 0
        self.is_shared = is_shared
        self.owner_id = owner_id or user_id
        self.is_admin = is_admin
        self.actual_index = actual_index
        self.indices = list(range(len(vps_list)))
        if len(vps_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"VPS {i+1} ({v.get('config', 'Custom')})",
                    description=f"Status: {v.get('status', 'unknown')}",
                    value=str(i)
                ) for i, v in enumerate(vps_list)
            ]
            self.select = discord.ui.Select(placeholder="Select VPS", options=options)
            self.select.callback = self.select_vps
            self.add_item(self.select)
        
        self.add_action_buttons()

    def add_action_buttons(self):
        start_button = discord.ui.Button(label="▶ Start", style=discord.ButtonStyle.success)
        start_button.callback = lambda inter: self.action_callback(inter, 'start')
        
        stop_button = discord.ui.Button(label="⏸ Stop", style=discord.ButtonStyle.secondary)
        stop_button.callback = lambda inter: self.action_callback(inter, 'stop')
        
        putty_button = discord.ui.Button(label="💻 PuTTY SSH", style=discord.ButtonStyle.primary)
        putty_button.callback = lambda inter: self.action_callback(inter, 'putty')
        
        stats_button = discord.ui.Button(label="📊 Stats", style=discord.ButtonStyle.secondary)
        stats_button.callback = lambda inter: self.action_callback(inter, 'stats')
        
        # New Delete Button Added
        delete_button = discord.ui.Button(label="🗑️️ Delete VPS", style=discord.ButtonStyle.danger)
        delete_button.callback = lambda inter: self.action_callback(inter, 'delete')

        self.add_item(start_button)
        self.add_item(stop_button)
        self.add_item(putty_button)
        self.add_item(stats_button)
        self.add_item(delete_button)

    async def get_initial_embed(self):
        return await self.create_vps_embed(self.selected_index)

    async def create_vps_embed(self, index):
        vps = self.vps_list[index]
        container_name = vps['container_name']
        embed = create_embed(f"Managing: `{container_name}`", f"Status: `{vps.get('status', 'unknown').upper()}`")
        add_field(embed, "Specs", vps.get('config', 'Custom'), True)
        add_field(embed, "OS", vps.get('os_version', 'ubuntu:22.04'), True)
        return embed

    async def select_vps(self, interaction: discord.Interaction):
        self.selected_index = int(self.select.values[0])
        await interaction.response.defer()
        new_embed = await self.create_vps_embed(self.selected_index)
        await interaction.edit_original_response(embed=new_embed, view=self)

    async def action_callback(self, interaction: discord.Interaction, action: str):
        actual_idx = self.actual_index if self.is_shared else self.indices[self.selected_index]
        target_vps = vps_data[self.owner_id][actual_idx]
        container_name = target_vps["container_name"]
        node_id = target_vps['node_id']

        if action == 'start':
            await interaction.response.defer(ephemeral=True)
            try:
                await safe_start_container(container_name, node_id)
                target_vps["status"] = "running"
                save_vps_data()
                await interaction.followup.send(embed=create_success_embed("Started", f"VPS `{container_name}` is online!"), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Failed", str(e)), ephemeral=True)

        elif action == 'stop':
            await interaction.response.defer(ephemeral=True)
            try:
                await execute_lxc(container_name, f"stop {container_name}", timeout=60, node_id=node_id)
                target_vps["status"] = "stopped"
                save_vps_data()
                await interaction.followup.send(embed=create_success_embed("Stopped", f"VPS `{container_name}` stopped."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Failed", str(e)), ephemeral=True)

        elif action == 'putty':
            await interaction.response.defer(ephemeral=True)
            root_pass = target_vps.get('root_password')
            if not root_pass:
                root_pass = await setup_ssh_access(container_name, node_id)
                target_vps['root_password'] = root_pass
                save_vps_data()

            pinggy_address = await establish_pinggy_tunnel(container_name, node_id)
            target_vps['pinggy_address'] = pinggy_address
            save_vps_data()

            embed = create_info_embed(f"💻 SSH Details - `{container_name}`")
            if pinggy_address:
                host, port = pinggy_address.split(":")
                add_field(embed, "Host", f"`{host}`", True)
                add_field(embed, "Port", f"`{port}`", True)
                add_field(embed, "Username", "`root`", True)
                add_field(embed, "Password", f"`{root_pass}`", True)
                add_field(embed, "PuTTY Cmd", f"```putty.exe -ssh root@{host} -P {port} -pw {root_pass}```", False)
            else:
                add_field(embed, "Password", f"`{root_pass}`", False)
                add_field(embed, "Tunnel Status", "Failed to get tunnel address. Make sure network is up.", False)
            await interaction.followup.send(embed=embed, ephemeral=True)

        elif action == 'stats':
            await interaction.response.defer(ephemeral=True)
            stats = await get_container_stats(container_name, node_id)
            embed = create_info_embed(f"Stats - `{container_name}`")
            add_field(embed, "CPU", f"{stats['cpu']}%", True)
            add_field(embed, "Uptime", f"{stats['uptime']}", True)
            await interaction.followup.send(embed=embed, ephemeral=True)

        elif action == 'delete':
            confirm_view = DeleteConfirmView(self.owner_id, container_name, node_id, actual_idx, self)
            confirm_embed = create_embed(
                "Confirm Delete",
                f"⚠️ **Are you sure you want to permanently delete `{container_name}`?**\nThis action cannot be undone!",
                color=0xff0000
            )
            await interaction.response.send_message(embed=confirm_embed, view=confirm_view, ephemeral=True)

@bot.command(name='create')
@is_admin()
async def create_vps(ctx, ram: int, cpu: int, disk: int, user: discord.Member):
    embed = create_info_embed("VPS Creation", f"Creating VPS for {user.mention}...")
    view = NodeSelectView(ram, cpu, disk, user, ctx)
    await ctx.send(embed=embed, view=view)

@bot.command(name='manage')
async def manage_vps(ctx, user: discord.Member = None):
    target_id = str(user.id) if user else str(ctx.author.id)
    vps_list = vps_data.get(target_id, [])
    if not vps_list:
        await ctx.send(embed=create_error_embed("No VPS", "No active VPS found for this user."))
        return
    view = ManageView(target_id, vps_list, is_admin=bool(user))
    embed = await view.get_initial_embed()
    await ctx.send(embed=embed, view=view)

if __name__ == '__main__':
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
