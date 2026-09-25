import os
import tempfile
import unittest
from pathlib import Path
import core

class SecurityTests(unittest.TestCase):
    def config(self):
        return dict(server='https://axis-pve:8006', pool='spice-access', token_id='spice-launcher@pve!desktop', ca_file='/etc/pve/pve-root-ca.pem')

    def test_reject_plain_http_and_embedded_credentials(self):
        for url in ('http://axis-pve:8006', 'https://user:pass@axis-pve:8006', 'https://axis-pve:8006/path'):
            with self.assertRaises(ValueError):
                core.validate_settings(dict(self.config(), server=url))

    def test_only_running_qemu_members_are_connectable(self):
        rows = core.vm_rows([dict(type='qemu', vmid=100, name='Kali', node='axis-pve', status='running'), dict(type='lxc', vmid=101), dict(type='qemu', vmid=102, node='axis-pve', status='stopped')])
        self.assertEqual([r['vmid'] for r in rows], [100, 102])
        self.assertFalse(rows[1]['status'] == 'running')

    def ticket(self):
        return {'type':'spice', 'host':'pvespiceproxy:abc', 'proxy':'http://axis-pve:3128', 'tls-port':61000, 'password':'secret', 'ca':'CERT', 'host-subject':'CN=axis-pve', 'delete-this-file':1}

    def test_ticket_rejects_redirected_proxy_and_newline_injection(self):
        for changes in ({'proxy':'http://evil:3128'}, {'password':'x\nproxy=http://evil'}, {'type':'vnc'}, {'host':'evil.example'}):
            with self.assertRaises(ValueError):
                core.viewer_text(dict(self.ticket(), **changes), 'axis-pve')

    def test_secret_and_ticket_files_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'secret'
            core.private_write(path, 'secret')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), 'secret')
            text = core.viewer_text(self.ticket(), 'axis-pve')
            self.assertTrue(text.startswith('[virt-viewer]\n'))
            self.assertIn('delete-this-file=1', text)

    def test_redirects_are_not_followed(self):
        with self.assertRaises(core.LauncherError):
            core.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil/')


class ConnectionTests(unittest.TestCase):
    def test_removed_member_cannot_request_ticket(self):
        client = object.__new__(core.Client)
        client.members = lambda: []
        with self.assertRaisesRegex(core.LauncherError, 'no longer'):
            client.connect(100)

    def test_stopped_vm_cannot_request_ticket(self):
        client = object.__new__(core.Client)
        client.members = lambda: [dict(vmid=100, status='stopped')]
        with self.assertRaisesRegex(core.LauncherError, 'not running'):
            client.connect(100)

    def test_failed_viewer_cleans_up_private_file(self):
        from unittest.mock import patch
        import subprocess
        with tempfile.TemporaryDirectory() as runtime:
            class Viewer:
                def __init__(self, args, **kwargs):
                    self.path = Path(args[1])
                    assert self.path.stat().st_mode & 0o777 == 0o600
                    assert self.path.read_text() == 'test ticket'
                def wait(self): return 1
            with patch.dict(os.environ, {'XDG_RUNTIME_DIR': runtime}), patch.object(subprocess, 'Popen', Viewer):
                with self.assertRaisesRegex(core.LauncherError, 'could not connect'):
                    core.launch_viewer('test ticket')
            self.assertEqual(list(Path(runtime).iterdir()), [])

class DisplayTests(unittest.TestCase):
    def client(self, display):
        client = object.__new__(core.Client)
        client.settings = {'server': 'https://axis-pve:8006'}
        client.members = lambda: [dict(vmid=100, name='Test VM', node='axis-pve', status='running')]
        def request(path, data=None):
            if path == '/nodes/axis-pve/qemu/100/config':
                return {'vga': display}
            if path == '/nodes/axis-pve/qemu/100/spiceproxy':
                return SecurityTests().ticket()
            raise AssertionError(path)
        client.request = request
        return client

    def test_supported_displays_reach_viewer(self):
        from unittest.mock import patch
        for display in ('qxl', 'qxl2', 'qxl3', 'qxl4', 'virtio', 'virtio-gl', 'type=virtio-gl,memory=128', 'memory=128,type=virtio'):
            with self.subTest(display=display), patch.object(core, 'launch_viewer') as viewer:
                self.assertEqual(self.client(display).connect(100), 'Test VM')
                self.assertIn('[virt-viewer]', viewer.call_args.args[0])

    def test_unsupported_displays_rejected(self):
        from unittest.mock import patch
        for display in ('', 'std', 'none', 'serial0', 'qxl-invalid'):
            with self.subTest(display=display), patch.object(core, 'launch_viewer'), self.assertRaises(core.LauncherError):
                self.client(display).connect(100)

if __name__ == '__main__': unittest.main()
