#!/usr/bin/env python3
"""Consistent, root-only backup; briefly stops Marzban and then starts it."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import tarfile


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo')
    os.umask(0o077)
    compose = ['docker', 'compose', '-f', '/opt/marzban/docker-compose.yml']
    status = subprocess.check_output(compose + ['ps', '--status', 'running', '--services'], text=True)
    if 'marzban' not in status.splitlines():
        raise SystemExit('Marzban is not running; inspect it before backup')
    directory = Path('/var/backups/marzban')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    target = directory / datetime.now(timezone.utc).strftime('marzban-%Y%m%dT%H%M%S.%fZ.tar.gz')
    partial = target.with_suffix('.partial')
    try:
        subprocess.run(compose + ['stop', 'marzban'], check=True)
        with tarfile.open(partial, 'w:gz') as archive:
            for path in ('/opt/marzban', '/var/lib/marzban', '/etc/letsencrypt'):
                archive.add(path, arcname=path.lstrip('/'))
        partial.replace(target)
    finally:
        subprocess.run(compose + ['up', '-d', 'marzban'], check=True)
    print(target)
    print('Contains private keys and the database. Transfer to encrypted off-server storage.')


if __name__ == '__main__':
    main()
