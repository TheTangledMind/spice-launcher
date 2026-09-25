"""Proxmox API and private viewer-file handling; no GUI dependency."""
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'spice-launcher'
DEFAULTS = dict(server='https://axis-pve:8006', pool='spice-access', token_id='spice-launcher@pve!desktop', ca_file=str(CONFIG_DIR / 'proxmox-ca.pem'))

class LauncherError(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LauncherError('The API attempted a redirect. Check the configured server address.')

def validate_settings(settings):
    s = {key: str(settings.get(key, '')).strip() for key in DEFAULTS}
    u = urllib.parse.urlsplit(s['server'])
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.path not in ('', '/') or u.query or u.fragment:
        raise ValueError('Use an HTTPS server address only, for example https://axis-pve:8006.')
    if any(c.isspace() for c in s['server']):
        raise ValueError('Server address cannot contain whitespace.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', s['pool']):
        raise ValueError('Enter a simple pool name (letters, numbers, dots, dashes, underscores).')
    if not re.fullmatch(r'[^\s=!]+@[^\s=!]+![^\s=!]+', s['token_id']):
        raise ValueError('Token ID must look like user@pve!token-name.')
    if not s['ca_file']:
        raise ValueError('Choose the Proxmox CA certificate file.')
    s['server'] = s['server'].rstrip('/')
    return s

def private_write(path, contents):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.new-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(contents)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def load_settings():
    try:
        return validate_settings(json.loads((CONFIG_DIR / 'settings.json').read_text()))
    except FileNotFoundError:
        return dict(DEFAULTS)

def save_settings(settings, secret):
    settings = validate_settings(settings)
    if not secret or any(c.isspace() for c in secret):
        raise ValueError('Enter a valid API token secret without whitespace.')
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    private_write(CONFIG_DIR / 'token', secret)
    private_write(CONFIG_DIR / 'settings.json', json.dumps(settings, indent=2) + '\n')
    return settings

def load_secret():
    path = CONFIG_DIR / 'token'
    try:
        st = path.lstat()
        if path.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise LauncherError('Token file must belong to you and have permissions 600.')
        return path.read_text().strip()
    except FileNotFoundError:
        raise LauncherError('Open Settings and enter your API token first.') from None

def vm_rows(members):
    return sorted([dict(m, vmid=int(m['vmid']), name=m.get('name') or 'Unnamed VM', status=m.get('status', 'unknown'))
                   for m in members if m.get('type') == 'qemu'], key=lambda m: m['vmid'])

class Client:
    def __init__(self, settings):
        self.settings = validate_settings(settings)
        self.secret = load_secret()
        try:
            context = ssl.create_default_context(cafile=self.settings['ca_file'])
        except (OSError, ssl.SSLError):
            raise LauncherError('Cannot read the CA certificate. Check its path in Settings.') from None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect())

    def request(self, path, data=None):
        headers = {'Authorization': 'PVEAPIToken=' + self.settings['token_id'] + '=' + self.secret}
        body = None if data is None else urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(self.settings['server'] + '/api2/json' + path, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=12) as response:
                return json.load(response)['data']
        except urllib.error.HTTPError as e:
            messages = {401:'Token rejected. Check the token ID and secret.', 403:'Permission denied. Check the user and token pool permissions.', 404:'Resource not found. Refresh the VM list and check the pool name.'}
            raise LauncherError(messages.get(e.code, f'Proxmox returned HTTP {e.code}. Check that the VM is running and supports SPICE.')) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                raise LauncherError('Certificate verification failed. Use the hostname on the Proxmox certificate and the correct CA.') from None
            raise LauncherError('Cannot reach Proxmox. Check the server address and network connection.') from None
        except (ValueError, KeyError):
            raise LauncherError('Proxmox returned an unexpected response.') from None

    def members(self):
        query = urllib.parse.urlencode({'poolid':self.settings['pool'], 'type':'qemu'})
        pools = self.request('/pools?' + query)
        if not pools: raise LauncherError('Pool not found or not visible to this token.')
        return vm_rows(pools[0].get('members', []))

    def connect(self, vmid, opened=None):
        # A fresh membership check matters here: the menu may have been left open.
        vm = next((m for m in self.members() if m['vmid'] == vmid), None)
        if vm is None: raise LauncherError('This VM is no longer in the configured pool. Refresh the list.')
        if vm['status'] != 'running': raise LauncherError('This VM is not running. Start it in Proxmox first.')
        node = urllib.parse.quote(vm['node'], safe='')
        path = f'/nodes/{node}/qemu/{vmid}'
        config = self.request(path + '/config')
        display_options = dict(part.split('=', 1) if '=' in part else ('type', part)
                               for part in str(config.get('vga', '')).split(','))
        if display_options.get('type') not in {'qxl', 'qxl2', 'qxl3', 'qxl4', 'virtio', 'virtio-gl'}:
            raise LauncherError('SPICE requires a supported display. In Proxmox, choose SPICE, VirtIO-GPU or VirGL GPU under VM → Hardware → Display.')
        proxy = urllib.parse.urlsplit(self.settings['server']).hostname
        ticket = self.request(path + '/spiceproxy', {'proxy':proxy})
        launch_viewer(viewer_text(ticket, proxy), opened)
        return vm['name']

def viewer_text(ticket, proxy):
    expected = f'http://[{proxy}]:3128' if ':' in proxy else f'http://{proxy}:3128'
    if ticket.get('type') != 'spice' or ticket.get('proxy') != expected or not str(ticket.get('host', '')).startswith('pvespiceproxy:'):
        raise ValueError('Unexpected SPICE destination in the server response.')
    keys = ('type', 'host', 'proxy', 'tls-port', 'password', 'ca', 'host-subject', 'title', 'secure-attention', 'toggle-fullscreen', 'release-cursor')
    lines = ['[virt-viewer]']
    for key in keys:
        value = str(ticket.get(key, ''))
        if key in ('tls-port', 'password', 'ca', 'host-subject') and not value:
            raise ValueError('Incomplete SPICE configuration returned by Proxmox.')
        if '\n' in value or '\r' in value or '\x00' in value:
            raise ValueError('Invalid line break in the SPICE configuration.')
        if value: lines.append(f'{key}={value}')
    lines.append('delete-this-file=1')
    return '\n'.join(lines) + '\n'

def launch_viewer(text, opened=None):
    base = os.environ.get('XDG_RUNTIME_DIR')
    if not base: raise LauncherError('No desktop runtime directory is available. Run from your logged-in desktop.')
    root = Path(base)
    st = root.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise LauncherError('Your desktop runtime directory is not private.')
    # Unique directory avoids collisions between simultaneous console sessions.
    with tempfile.TemporaryDirectory(prefix='spice-launcher-', dir=root) as directory:
        path = Path(directory) / 'console.vv'
        private_write(path, text)
        try:
            process = subprocess.Popen(['/usr/bin/remote-viewer', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if opened: opened()
            returncode = process.wait()
        except OSError:
            raise LauncherError('Could not start /usr/bin/remote-viewer.') from None
        if returncode:
            raise LauncherError('The viewer could not connect. Try again for a fresh ticket; check SPICE and proxy connectivity if it persists.')
