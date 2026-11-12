"""Offline tests; never execute host installation commands."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import deploy
import backup


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'deploy.json'
        self.c = json.loads((Path(__file__).parent.parent / 'assets/deploy.json.example').read_text())
        self.c.update(domain='vpn.example.org', email='admin@example.org', admin_user='vpnadmin',
                      ssh_port=22022, panel_port=38443, reality_target='www.example.net')
        (self.path.parent / 'admin.pub').write_text('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestFixture user')

    def load(self):
        self.path.write_text(json.dumps(self.c))
        return deploy.load_config(self.path)

    def test_valid_configuration_resolves_relative_key(self):
        self.assertIn('public_key', self.load())

    def test_unfilled_template_rejected(self):
        self.c = json.loads((Path(__file__).parent.parent / 'assets/deploy.json.example').read_text())
        with self.assertRaises(ValueError):
            self.load()

    def test_reality_target_has_no_default(self):
        del self.c['reality_target']
        with self.assertRaises(ValueError):
            self.load()

    def test_reject_bad_domains(self):
        for domain in ('*.example.org', 'localhost', '192.0.2.1',
                       'vpn.example.org\nEVIL=1', '-a.example.org'):
            with self.subTest(domain=domain):
                self.c['domain'] = domain
                with self.assertRaises(ValueError):
                    self.load()

    def test_custom_domain_and_target(self):
        self.c['domain'] = 'vpn.example.org'
        self.c['reality_target'] = 'www.example.net'
        c = self.load()
        cfg = deploy.xray_config('test-private', '0123456789abcdef', c['reality_target'])
        self.assertEqual(cfg['inbounds'][0]['streamSettings']['realitySettings']['dest'], 'www.example.net:443')

    def test_target_rejects_urls_and_command_injection(self):
        for target in ('https://www.example.net', 'www.example.net:443', 'example.org\nPort 22', '*.example.org'):
            self.c['reality_target'] = target
            with self.assertRaises(ValueError):
                self.load()

    def test_reject_ports(self):
        for port in (True, '38443', 8000, 22022, 65536, 80):
            with self.subTest(port=port):
                self.c['panel_port'] = port
                with self.assertRaises(ValueError):
                    self.load()

    def test_reject_mutable_images_and_shell_input(self):
        for image in ('gozargah/marzban:dev', 'gozargah/marzban:latest',
                      'gozargah/marzban:v0.8.4;reboot'):
            self.c['image'] = image
            with self.assertRaises(ValueError):
                self.load()

    def test_private_key_rejected(self):
        (self.path.parent / 'admin.pub').write_text('-----BEGIN OPENSSH PRIVATE KEY-----')
        with self.assertRaises(ValueError):
            self.load()

    def test_wrong_session_rejected(self):
        c = self.load()
        for user, port in (('root', '22022'), ('vpnadmin', '22')):
            with patch.dict(os.environ, {'SUDO_USER': user, 'SSH_CONNECTION': f'1 123 2 {port}'}):
                with self.assertRaises(RuntimeError):
                    deploy.require_new_session(c)

    def test_new_session_accepted(self):
        with patch.dict(os.environ, {'SUDO_USER': 'vpnadmin', 'SSH_CONNECTION': '1 123 2 22022'}):
            deploy.require_new_session(self.load())

    def test_panel_and_subscription_ports_consistent(self):
        c = self.load()
        env = deploy.env_config(c)
        self.assertIn('https://vpn.example.org:38443', env)
        self.assertIn('sqlite:////var/lib/marzban/db.sqlite3', env)

    def test_xray_has_no_unused_inbounds_and_keeps_target(self):
        config = deploy.xray_config('test-private', '0123456789abcdef', 'www.example.net')
        self.assertEqual(len(config['inbounds']), 1)
        inbound = config['inbounds'][0]
        self.assertEqual((inbound['port'], inbound['protocol']), (443, 'vless'))
        self.assertEqual(inbound['streamSettings']['realitySettings']['serverNames'], ['www.example.net'])
        self.assertEqual(inbound['streamSettings']['realitySettings']['dest'], 'www.example.net:443')
        self.assertEqual(inbound['settings']['clients'], [])

    def test_rollback_scheduled_before_ssh_mutation(self):
        c = self.load()
        c['phase'] = 'prepared'
        events = []
        def fake_run(*args, **kwargs):
            events.append(('run', args))
            return subprocess.CompletedProcess(args, 0, '')
        def fake_write(*args):
            events.append(('write', args))
        with patch.object(deploy, 'require_new_session'), patch.object(deploy, 'save'), \
             patch.object(deploy, 'write', side_effect=fake_write), \
             patch.object(deploy, 'run', side_effect=fake_run):
            deploy.finalize(c)
        timer = next(i for i, e in enumerate(events) if e[0] == 'run' and e[1][0] == 'systemd-run')
        mutation = next(i for i, e in enumerate(events) if e[0] == 'write')
        self.assertLess(timer, mutation)
        self.assertEqual(c['phase'], 'awaiting-confirm')

    def test_failure_to_schedule_rollback_leaves_ssh_untouched(self):
        c = self.load()
        c['phase'] = 'prepared'
        def fake_run(*args, **kwargs):
            if args[0] == 'systemd-run':
                raise subprocess.CalledProcessError(1, args)
        with patch.object(deploy, 'require_new_session'), patch.object(deploy, 'save') as save, \
             patch.object(deploy, 'write') as write, patch.object(deploy, 'run', side_effect=fake_run):
            with self.assertRaises(subprocess.CalledProcessError):
                deploy.finalize(c)
            write.assert_not_called()
            save.assert_not_called()

    def test_subprocess_stdout_override(self):
        with patch('subprocess.run') as run:
            deploy.run('test', stdout=subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs['stdout'], subprocess.DEVNULL)

    def test_backup_restarts_container_after_archive_failure(self):
        with patch.object(backup.os, 'geteuid', return_value=0, create=True), \
             patch.object(backup.os, 'umask'), \
             patch.object(backup.Path, 'mkdir'), patch.object(backup.Path, 'chmod'), \
             patch.object(backup.subprocess, 'check_output', return_value='marzban\n'), \
             patch.object(backup.subprocess, 'run') as run, \
             patch.object(backup.tarfile, 'open', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                backup.main()
        self.assertEqual(run.call_args_list[-1].args[0][-3:], ['up', '-d', 'marzban'])

    def test_finalize_wrong_phase_leaves_services_untouched(self):
        c = self.load()
        c['phase'] = 'preparing'
        with patch.object(deploy, 'require_new_session'), patch.object(deploy, 'run') as run:
            with self.assertRaises(RuntimeError):
                deploy.finalize(c)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
