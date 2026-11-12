#!/usr/bin/env python3
"""Fresh Ubuntu 24.04/26.04 deployment. Never run prepare on an existing VPN server."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time

STATE = Path('/var/lib/marzban-bootstrap')
APP = Path('/opt/marzban')
DATA = Path('/var/lib/marzban')
SELF = '/usr/local/sbin/marzban-deploy'
COMPOSE = ['docker', 'compose', '-f', str(APP / 'docker-compose.yml')]


def run(*args, capture=False, check=True, **kwargs):
    kwargs.setdefault('stdout', subprocess.PIPE if capture else None)
    return subprocess.run(list(args), check=check, text=True, **kwargs)


def write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(content, encoding='utf-8', newline='\n')
    temp.chmod(mode)
    temp.replace(path)


def load_config(path):
    path = Path(path)
    c = json.loads(path.read_text(encoding='utf-8-sig'))
    required = {'domain', 'email', 'admin_user', 'ssh_public_key_file',
                'ssh_port', 'panel_port', 'image', 'reality_target'}
    if set(c) != required:
        raise ValueError('Configuration keys must be: ' + ', '.join(sorted(required)))
    if any(c[k] is None for k in required):
        raise ValueError('Fill every configuration field before deployment')
    def hostname(value):
        return isinstance(value, str) and len(value) <= 253 and '.' in value and all(
            re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in value.split('.')) and not value.replace('.', '').isdigit()
    if not hostname(c['domain']):
        raise ValueError('Use a concrete public DNS hostname, not a wildcard or IP')
    if not hostname(c['reality_target']):
        raise ValueError('REALITY target must be a DNS hostname without URL scheme or port')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', c['email']):
        raise ValueError('Invalid email')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', c['admin_user']) or c['admin_user'] == 'root':
        raise ValueError('Invalid administrator username')
    for key in ('ssh_port', 'panel_port'):
        if type(c[key]) is not int or not 1024 <= c[key] <= 65535:
            raise ValueError('Ports must be integers in 1024..65535')
    if c['ssh_port'] == c['panel_port'] or c['panel_port'] == 8000:
        raise ValueError('Ports must differ; panel port 8000 is not allowed')
    if not re.fullmatch(r'gozargah/marzban:(?:v\d+\.\d+\.\d+)|gozargah/marzban@sha256:[a-f0-9]{64}', c['image']):
        raise ValueError('Use a release tag or digest, not latest/dev')
    key = (path.parent / c.pop('ssh_public_key_file')).read_text().strip()
    if '\n' in key or not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+(?: [^\r\n]*)?', key):
        raise ValueError('Expected one OpenSSH public key, not a private key')
    c['public_key'] = key
    return c


def ssh_config(c, temporary=False):
    return f'''Port {c['ssh_port']}
ListenAddress 0.0.0.0
ListenAddress ::
PidFile /run/sshd{'-bootstrap' if temporary else ''}.pid
PermitRootLogin no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
UsePAM yes
AuthorizedKeysFile .ssh/authorized_keys
AllowUsers {c['admin_user']}
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding local
PermitTunnel no
Subsystem sftp internal-sftp
'''


def xray_config(private_key, short_id, target):
    return {
        'log': {'loglevel': 'warning'},
        'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [
            {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'BLOCK'}]},
        'inbounds': [{'tag': 'VLESS TCP REALITY', 'listen': '0.0.0.0',
                      'port': 443, 'protocol': 'vless',
                      'settings': {'clients': [], 'decryption': 'none'},
                      'streamSettings': {'network': 'tcp', 'security': 'reality',
                          'realitySettings': {'show': False, 'dest': target + ':443',
                              'xver': 0, 'serverNames': [target],
                              'privateKey': private_key, 'shortIds': [short_id]}},
                      'sniffing': {'enabled': True, 'routeOnly': True,
                                   'destOverride': ['http', 'tls']}}],
        'outbounds': [{'protocol': 'freedom', 'tag': 'DIRECT'},
                      {'protocol': 'blackhole', 'tag': 'BLOCK'}]}


def env_config(c):
    return f'''UVICORN_HOST=0.0.0.0
UVICORN_PORT={c['panel_port']}
UVICORN_SSL_CERTFILE=/var/lib/marzban/certs/fullchain.pem
UVICORN_SSL_KEYFILE=/var/lib/marzban/certs/key.pem
UVICORN_SSL_CA_TYPE=public
XRAY_JSON=/var/lib/marzban/xray_config.json
SQLALCHEMY_DATABASE_URL=sqlite:////var/lib/marzban/db.sqlite3
XRAY_SUBSCRIPTION_URL_PREFIX=https://{c['domain']}:{c['panel_port']}
'''


def save(c):
    write(STATE / 'state.json', json.dumps(c, indent=2))


def listening_ports():
    result = run('ss', '-H', '-lnt', capture=True).stdout
    return {int(line.split()[3].rsplit(':', 1)[1]) for line in result.splitlines()}


def check_target(target):
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(['h2'])
    with socket.create_connection((target, 443), timeout=15) as sock:
        with context.wrap_socket(sock, server_hostname=target) as conn:
            if conn.selected_alpn_protocol() != 'h2':
                raise RuntimeError(target + ' did not negotiate h2 from this VPS')
    print(target + ': TLS 1.3, trusted hostname certificate, h2 OK')


def prepare(c, sudo_password=None):
    if sudo_password is not None and (not sudo_password or any(char in sudo_password for char in '\r\n\x00')):
        raise ValueError('Invalid sudo password')
    os_release = Path('/etc/os-release').read_text()
    if not re.search(r'^ID=ubuntu$', os_release, re.M) or not re.search(r'^VERSION_ID="(?:24|26)\.04"$', os_release, re.M):
        raise RuntimeError('This installer supports clean Ubuntu 24.04/26.04 LTS only')
    for path in (APP, DATA, STATE, Path('/usr/local/x-ui'), Path('/etc/x-ui'),
                 Path('/root/.acme.sh'), Path('/etc/letsencrypt/renewal')):
        if path.exists():
            raise RuntimeError(f'Existing installation/state found: {path}; no files overwritten')
    if run('getent', 'passwd', c['admin_user'], check=False, capture=True).returncode == 0:
        raise RuntimeError('Administrator already exists; use a clean server')
    # Keep all currently configured SSH ports until confirm, including nonstandard ones.
    ssh = run('/usr/sbin/sshd', '-T', capture=True).stdout
    c['old_ports'] = sorted({int(line.split()[1]) for line in ssh.splitlines() if line.startswith('port ')})
    connection = os.environ.get('SSH_CONNECTION', '').split()
    if len(connection) == 4:
        c['old_ports'] = sorted(set(c['old_ports']) | {int(connection[3])})
    busy = listening_ports() & {80, 443, c['ssh_port'], c['panel_port']}
    if busy or c['ssh_port'] in c['old_ports'] or c['panel_port'] in c['old_ports']:
        raise RuntimeError(f'Required ports conflict with existing listeners/SSH: {busy}')
    if shutil.which('docker') or shutil.which('certbot'):
        raise RuntimeError('Existing Docker/Certbot detected; this is a fresh-server installer')
    if run('systemctl', 'show', 'ssh.service', '-p', 'DropInPaths', '--value', capture=True).stdout.strip():
        raise RuntimeError('Custom ssh.service overrides found; review manually')
    defaults = Path('/etc/default/ssh')
    if defaults.exists() and re.search(r'^SSHD_OPTS\s*=\s*[\"\']?[^\s\"\'\n]', defaults.read_text(), re.M):
        raise RuntimeError('Custom SSHD_OPTS found; review manually')
    if shutil.which('ufw'):
        status = run('ufw', 'status', capture=True).stdout
        added = run('ufw', 'show', 'added', capture=True).stdout
        if 'Status: active' in status or re.search(r'^ufw ', added, re.M):
            raise RuntimeError('Existing firewall rules found; review manually')
    # DNS must already point at this VPS; ACME validates actual reachability later.
    socket.getaddrinfo(c['domain'], 80)
    check_target(c['reality_target'])
    STATE.mkdir(mode=0o700)
    write(STATE / 'admin.pub', c['public_key'] + '\n')
    run('ssh-keygen', '-l', '-f', str(STATE / 'admin.pub'), capture=True)
    c['phase'] = 'preparing'
    c['socket_active'] = run('systemctl', 'is-active', '--quiet', 'ssh.socket', check=False).returncode == 0
    c['socket_enabled'] = run('systemctl', 'is-enabled', '--quiet', 'ssh.socket', check=False).returncode == 0
    c['service_enabled'] = run('systemctl', 'is-enabled', '--quiet', 'ssh.service', check=False).returncode == 0
    save(c)
    shutil.copy2('/etc/ssh/sshd_config', STATE / 'sshd_config.original')
    write(SELF, Path(__file__).read_text(), 0o700)
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'docker.io', 'docker-compose-v2', 'certbot',
        'ufw', 'fail2ban', 'unattended-upgrades', 'openssl', 'curl', 'sudo')
    run('systemctl', 'enable', '--now', 'docker')
    # A local sudo password is distinct from SSH authentication (which is key-only).
    run('useradd', '--create-home', '--shell', '/bin/bash', '--groups', 'sudo', c['admin_user'])
    if sudo_password is None:
        print('Set a local sudo password for the new administrator (not an SSH password).')
        run('passwd', c['admin_user'])
    else:
        run('chpasswd', input=f"{c['admin_user']}:{sudo_password}\n")
    home = Path('/home') / c['admin_user']
    write(home / '.ssh/authorized_keys', c['public_key'] + '\n')
    (home / '.ssh').chmod(0o700)
    run('chown', '-R', f"{c['admin_user']}:{c['admin_user']}", str(home / '.ssh'))
    write('/etc/ssh/sshd_config.bootstrap', ssh_config(c, True))
    run('/usr/sbin/sshd', '-t', '-f', '/etc/ssh/sshd_config.bootstrap')
    write('/etc/systemd/system/ssh-bootstrap.service', '''[Unit]
Description=Temporary key-only SSH during Marzban setup
After=network.target
[Service]
ExecStartPre=/usr/bin/mkdir -p /run/sshd
ExecStart=/usr/sbin/sshd -D -f /etc/ssh/sshd_config.bootstrap
KillMode=process
Restart=on-failure
[Install]
WantedBy=multi-user.target
''', 0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'ssh-bootstrap.service')
    run('ufw', 'default', 'deny', 'incoming')
    run('ufw', 'default', 'allow', 'outgoing')
    for port in sorted(set(c['old_ports']) | {80, 443, c['ssh_port'], c['panel_port']}):
        run('ufw', 'allow', f'{port}/tcp')
    run('ufw', '--force', 'enable')
    # Use Ubuntu's Certbot package/timer rather than running a downloaded installer as root.
    run('certbot', 'certonly', '--standalone', '--non-interactive', '--agree-tos',
        '--email', c['email'], '--cert-name', c['domain'], '-d', c['domain'],
        '--key-type', 'ecdsa')
    c['phase'] = 'certificate-issued'
    save(c)
    DATA.mkdir(mode=0o700)
    APP.mkdir(mode=0o700)
    hook = f'''#!/bin/sh
set -eu
[ "${{RENEWED_LINEAGE:-}}" = "/etc/letsencrypt/live/{c['domain']}" ] || exit 0
install -d -m 700 /var/lib/marzban/certs
install -m 600 "$RENEWED_LINEAGE/privkey.pem" /var/lib/marzban/certs/key.pem.new
install -m 644 "$RENEWED_LINEAGE/fullchain.pem" /var/lib/marzban/certs/fullchain.pem.new
mv /var/lib/marzban/certs/key.pem.new /var/lib/marzban/certs/key.pem
mv /var/lib/marzban/certs/fullchain.pem.new /var/lib/marzban/certs/fullchain.pem
if [ -f /opt/marzban/docker-compose.yml ]; then
  /usr/bin/docker compose -f /opt/marzban/docker-compose.yml restart marzban
fi
'''
    write('/etc/letsencrypt/renewal-hooks/deploy/marzban', hook, 0o700)
    run('/etc/letsencrypt/renewal-hooks/deploy/marzban',
        env={**os.environ, 'RENEWED_LINEAGE': '/etc/letsencrypt/live/' + c['domain']})
    run('systemctl', 'enable', '--now', 'certbot.timer')
    run('docker', 'pull', c['image'])
    digests = json.loads(run('docker', 'image', 'inspect', c['image'],
                            '--format', '{{json .RepoDigests}}', capture=True).stdout)
    c['image_digest'] = next(d for d in digests if d.startswith('gozargah/marzban@sha256:'))
    write(APP / 'docker-compose.yml', f'''services:
  marzban:
    image: {c['image_digest']}
    restart: unless-stopped
    env_file: .env
    network_mode: host
    volumes:
      - /var/lib/marzban:/var/lib/marzban
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
''')
    write(APP / '.env', env_config(c))
    keys = run('docker', 'run', '--rm', '--network', 'none', '--entrypoint',
               '/usr/local/bin/xray', c['image_digest'], 'x25519', capture=True).stdout
    match = re.search(r'^Private\s*Key:\s*(\S+)', keys, re.I | re.M)
    if not match:
        raise RuntimeError('Unexpected Xray key output; private material is not printed')
    write(DATA / 'xray_config.json', json.dumps(xray_config(match[1], secrets.token_hex(8), c['reality_target']), indent=2))
    run(*COMPOSE, 'config', '--quiet')
    run(*COMPOSE, 'run', '--rm', '--no-deps', '--entrypoint', '/usr/local/bin/xray',
        'marzban', 'run', '-test', '-config', '/var/lib/marzban/xray_config.json')
    run(*COMPOSE, 'up', '-d')
    for _ in range(60):
        result = run('curl', '--silent', '--fail', '--noproxy', '*', '--max-time', '3',
                     '--resolve', f"{c['domain']}:{c['panel_port']}:127.0.0.1",
                     f"https://{c['domain']}:{c['panel_port']}/dashboard/",
                     capture=True, check=False)
        if result.returncode == 0 and 443 in listening_ports():
            break
        time.sleep(2)
    else:
        raise RuntimeError('Panel/Xray startup failed; inspect docker compose logs')
    write('/etc/fail2ban/jail.d/marzban-sshd.local', f'''[sshd]
enabled = true
backend = systemd
port = {c['ssh_port']}
maxretry = 4
findtime = 10m
bantime = 1h
banaction = ufw
''', 0o644)
    run('systemctl', 'enable', '--now', 'fail2ban')
    run('systemctl', 'restart', 'fail2ban')
    write('/etc/apt/apt.conf.d/20auto-upgrades', '''APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
''', 0o644)
    write('/etc/apt/apt.conf.d/52marzban-no-auto-reboot',
          'Unattended-Upgrade::Automatic-Reboot "false";\n', 0o644)
    c['phase'] = 'prepared'
    save(c)
    print(f"READY: https://{c['domain']}:{c['panel_port']}/dashboard/")
    print(f"Open a NEW key-only SSH session: ssh -p {c['ssh_port']} {c['admin_user']}@{c['domain']}")
    print(f"Then: sudo --preserve-env=SSH_CONNECTION python3 {SELF} finalize")
    print('Create panel admin: sudo docker compose -f /opt/marzban/docker-compose.yml exec marzban marzban-cli admin create --sudo')


def require_new_session(c):
    conn = os.environ.get('SSH_CONNECTION', '').split()
    if os.environ.get('SUDO_USER') != c['admin_user'] or len(conn) != 4 or conn[3] != str(c['ssh_port']):
        raise RuntimeError('Run from the new administrator SSH session using sudo --preserve-env=SSH_CONNECTION')


def rollback(c):
    run('systemctl', 'stop', 'marzban-ssh-rollback.timer', check=False)
    for port in c['old_ports']:
        run('ufw', 'allow', f'{port}/tcp')
    shutil.copy2(STATE / 'sshd_config.original', '/etc/ssh/sshd_config')
    run('systemctl', 'stop', 'ssh.service', check=False)
    run('systemctl', 'daemon-reload')
    if c['socket_enabled']:
        run('systemctl', 'enable', 'ssh.socket')
    if c['socket_active']:
        run('systemctl', 'start', 'ssh.socket')
    run('systemctl', 'restart', 'ssh.service')
    if not c['service_enabled']:
        run('systemctl', 'disable', 'ssh.service')
    run('systemctl', 'start', 'ssh-bootstrap.service')
    c['phase'] = 'prepared'
    save(c)
    print('Original SSH restored. Old firewall allowances retained.')


def finalize(c):
    require_new_session(c)
    if c['phase'] != 'prepared':
        raise RuntimeError('Expected prepared phase')
    # Schedule recovery BEFORE any change to the original SSH service.
    run('systemctl', 'reset-failed', 'marzban-ssh-rollback.service', check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run('systemd-run', '--unit=marzban-ssh-rollback', '--on-active=180s',
        '/usr/bin/python3', SELF, 'rollback')
    c['phase'] = 'awaiting-confirm'
    save(c)
    write('/etc/ssh/sshd_config', ssh_config(c), 0o644)
    run('/usr/sbin/sshd', '-t')
    run('systemctl', 'disable', '--now', 'ssh.socket')
    run('systemctl', 'stop', 'ssh-bootstrap.service')
    run('systemctl', 'enable', 'ssh.service')
    run('systemctl', 'restart', 'ssh.service')
    print('Within 180 seconds open ANOTHER new SSH session, then run:')
    print(f'sudo --preserve-env=SSH_CONNECTION python3 {SELF} confirm')


def confirm(c):
    require_new_session(c)
    if c['phase'] != 'awaiting-confirm':
        raise RuntimeError('No pending SSH transition')
    # Prove this login is served by the final ssh.service, not the old bootstrap daemon.
    pid = os.getpid()
    found = False
    while pid > 1:
        if '/ssh.service' in Path(f'/proc/{pid}/cgroup').read_text():
            found = True
        status = Path(f'/proc/{pid}/status').read_text()
        pid = int(re.search(r'^PPid:\s*(\d+)', status, re.M)[1])
    if not found:
        raise RuntimeError('Open a NEW SSH session after finalize; this is the bootstrap session')
    run('systemctl', 'stop', 'marzban-ssh-rollback.timer')
    if run('systemctl', 'is-active', '--quiet', 'marzban-ssh-rollback.service', check=False).returncode == 0:
        raise RuntimeError('Rollback is already running; wait and repeat finalize')
    run('systemctl', 'disable', '--now', 'ssh-bootstrap.service')
    for port in c['old_ports']:
        if port not in {c['ssh_port'], c['panel_port'], 80, 443}:
            run('ufw', '--force', 'delete', 'allow', f'{port}/tcp')
    c['phase'] = 'complete'
    save(c)
    run('ufw', 'status', 'verbose')
    print('SSH transition confirmed; original SSH ports closed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'prepare', 'finalize', 'confirm', 'rollback'])
    parser.add_argument('--config', default='deploy.json')
    parser.add_argument('--sudo-password-stdin', action='store_true', help='Read local sudo password from stdin, never command-line arguments')
    args = parser.parse_args()
    if args.action == 'check':
        c = load_config(args.config)
        print(json.dumps({k: v for k, v in c.items() if k != 'public_key'}, indent=2))
        print('Input structure valid. No server changes or network checks performed.')
        return
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise RuntimeError('Run as root on the target Ubuntu server')
    os.umask(0o077)
    os.environ['LC_ALL'] = 'C.UTF-8'
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    if args.action == 'prepare':
        sudo_password = sys.stdin.readline().rstrip('\r\n') if args.sudo_password_stdin else None
        prepare(load_config(args.config), sudo_password)
    else:
        c = json.loads((STATE / 'state.json').read_text())
        globals()[args.action](c)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, TypeError, OSError, subprocess.CalledProcessError, StopIteration) as exc:
        print(f'STOPPED: {exc}\nKeep the original SSH session open. See the guide for recovery.', file=sys.stderr)
        sys.exit(1)
