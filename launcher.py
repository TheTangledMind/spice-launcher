#!/usr/bin/env python3
"""Small native desktop launcher for a Proxmox SPICE pool."""
import sys
import threading
from pathlib import Path
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib
import core

CSS = b'''
window { background: #111823; color: #e8edf5; }
headerbar { background: #192332; border-bottom: 1px solid #2c3a4c; color: #eef4ff; }
label { color: #e8edf5; }
.title { font-size: 29px; font-weight: 800; }
.subtitle { color: #96a8bd; font-size: 13px; }
.eyebrow { color: #72d7c2; font-size: 11px; font-weight: bold; }
.card { background: #1b2635; border: 1px solid #304055; border-radius: 12px; padding: 17px; margin-bottom: 10px; }
.vm-name { font-size: 18px; font-weight: bold; }
.running { color: #79dec1; }
.stopped { color: #afbac9; }
button { background: #29394d; color: #e8edf5; border: 1px solid #41516a; border-radius: 7px; padding: 8px 15px; box-shadow: none; text-shadow: none; }
button:hover { background: #354b65; }
button.suggested-action { background: #71dbc0; color: #102c29; border: none; font-weight: bold; }
button:disabled { background: #233040; color: #78889c; border-color: #304055; }
entry { background: #192535; color: #edf4fc; border: 1px solid #3b4d64; border-radius: 7px; padding: 9px; }
scrolledwindow, viewport, list { background: transparent; }
.statusbar { background: #172130; padding: 12px; border-top: 1px solid #2c3a4c; }
.error { color: #ffb2a6; }
.dialog-vbox { background: #111823; }
'''

def label(text, style=None):
    item = Gtk.Label(label=text, xalign=0)
    item.set_line_wrap(True)
    if style: item.get_style_context().add_class(style)
    return item

class Window(Gtk.Window):
    def __init__(self, demo=False):
        super().__init__(title='SPICE Pool Launcher')
        self.set_default_size(760, 640)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect('destroy', Gtk.main_quit)
        self.demo = demo
        self.rows = []
        self.active = set()
        try: self.settings = core.load_settings()
        except (ValueError, OSError): self.settings = dict(core.DEFAULTS)
        header = Gtk.HeaderBar(title='SPICE Launcher', show_close_button=True)
        self.set_titlebar(header)
        settings = Gtk.Button(label='Settings')
        settings.connect('clicked', self.configure)
        header.pack_end(settings)
        self.refresh_button = Gtk.Button(label='↻  Refresh')
        self.refresh_button.connect('clicked', lambda *_: self.refresh())
        header.pack_end(self.refresh_button)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_border_width(26)
        outer.pack_start(content, True, True, 0)
        content.pack_start(label('LOCAL CONSOLES  /  PROXMOX VE', 'eyebrow'), False, False, 0)
        content.pack_start(label('Your machines. One click away.', 'title'), False, False, 0)
        self.description = label('', 'subtitle')
        content.pack_start(self.description, False, False, 0)
        self.search = Gtk.SearchEntry(placeholder_text='Find a machine by name or VM ID…')
        self.search.connect('search-changed', lambda *_: self.render())
        content.pack_start(self.search, False, False, 3)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scroll.add(self.cards)
        content.pack_start(scroll, True, True, 0)
        self.status = label('Ready', 'subtitle')
        self.status.get_style_context().add_class('statusbar')
        outer.pack_end(self.status, False, False, 0)
        self.show_all()
        if demo:
            self.rows = [dict(vmid=100, name='Kali Linux', node='axis-pve', status='running'), dict(vmid=101, name='Windows Lab', node='axis-pve', status='running'), dict(vmid=102, name='Forensics Sandbox', node='axis-pve', status='stopped')]
            self.render()
            self.set_status('Preview mode • Sample machines only • Connections disabled')
        elif (core.CONFIG_DIR / 'token').exists(): self.refresh()
        else:
            self.render()
            self.set_status('Welcome. Open Settings to add your Proxmox API token.')

    def set_status(self, message, error=False):
        self.status.set_text(message)
        ctx = self.status.get_style_context()
        (ctx.add_class if error else ctx.remove_class)('error')

    def work(self, task, done):
        def run():
            try:
                value = task()
                GLib.idle_add(done, value, None)
            except Exception as e:
                # Never show raw HTTP response bodies, which may contain tickets.
                message = str(e) if isinstance(e, (core.LauncherError, ValueError)) else 'Operation failed. Check your settings and try again.'
                GLib.idle_add(done, None, message)
        threading.Thread(target=run, daemon=False).start()

    def refresh(self):
        if self.demo: return
        self.refresh_button.set_sensitive(False)
        self.set_status('Loading pool members…')
        settings = dict(self.settings)
        def done(rows, error):
            self.refresh_button.set_sensitive(True)
            self.rows = rows or []
            self.render()
            self.set_status(error or f'{len(self.rows)} machines in pool • Membership managed in Proxmox', bool(error))
        self.work(lambda: core.Client(settings).members(), done)

    def render(self):
        for child in self.cards.get_children(): self.cards.remove(child)
        self.description.set_text(f"{self.settings['pool']}  •  {self.settings['server']}" + ('  •  PREVIEW' if self.demo else ''))
        query = self.search.get_text().lower()
        visible = [vm for vm in self.rows if query in f"{vm['name']} {vm['vmid']}".lower()]
        if not visible:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.get_style_context().add_class('card')
            box.pack_start(label('No matching machines' if self.rows else 'Your console collection starts here', 'vm-name'), False, False, 0)
            box.pack_start(label('Clear the search to see all machines.' if self.rows else 'Configure your connection in Settings, then add VMs to the pool in Proxmox and refresh.', 'subtitle'), False, False, 0)
            self.cards.pack_start(box, False, False, 0)
        for vm in visible:
            card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
            card.get_style_context().add_class('card')
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            info.pack_start(label(vm['name'], 'vm-name'), False, False, 0)
            info.pack_start(label(f"VM {vm['vmid']}  /  {vm['node']}", 'subtitle'), False, False, 0)
            info.pack_start(label('●  ' + vm['status'].capitalize(), 'running' if vm['status'] == 'running' else 'stopped'), False, False, 0)
            card.pack_start(info, True, True, 0)
            button = Gtk.Button(label='Console open' if vm['vmid'] in self.active else 'Connect  →')
            button.set_valign(Gtk.Align.CENTER)
            button.get_style_context().add_class('suggested-action')
            button.set_sensitive(vm['status'] == 'running' and vm['vmid'] not in self.active and not self.demo)
            button.set_tooltip_text('Open a fresh SPICE console' if vm['status'] == 'running' else 'Start this VM in Proxmox first')
            button.connect('clicked', lambda _, row=vm: self.connect_vm(row))
            card.pack_end(button, False, False, 0)
            event = Gtk.EventBox()
            event.add(card)
            event.connect('button-press-event', lambda _, event, row=vm: self.double_click(event, row))
            self.cards.pack_start(event, False, False, 0)
        self.cards.show_all()

    def double_click(self, event, vm):
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and event.button == 1:
            self.connect_vm(vm)

    def connect_vm(self, vm):
        if self.demo or vm['vmid'] in self.active or vm['status'] != 'running': return
        self.active.add(vm['vmid'])
        self.render()
        self.set_status(f"Opening {vm['name']}… The viewer opens in a separate window.")
        settings = dict(self.settings)
        def done(name, error):
            self.active.discard(vm['vmid'])
            self.render()
            self.set_status(error or f'{name} console closed.', bool(error))
        self.work(lambda: core.Client(settings).connect(vm['vmid'], lambda: GLib.idle_add(self.set_status, f"Viewer started for {vm['name']}. You can open another console.")), done)

    def configure(self, *_):
        dialog = Gtk.Dialog(title='Connection settings', transient_for=self, modal=True)
        dialog.set_default_size(560, 420)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Save & refresh', Gtk.ResponseType.OK)
        body = dialog.get_content_area()
        body.set_border_width(22)
        body.set_spacing(10)
        body.pack_start(label('Connect to your pool', 'vm-name'), False, False, 0)
        body.pack_start(label('Use a restricted API token. Credentials stay in your user configuration directory.', 'subtitle'), False, False, 0)
        fields = {}
        for key, title in [('server','Proxmox HTTPS address'), ('pool','Resource pool'), ('token_id','API token ID'), ('ca_file','Trusted CA certificate')]:
            body.pack_start(label(title, 'subtitle'), False, False, 0)
            fields[key] = Gtk.Entry(text=self.settings[key])
            body.pack_start(fields[key], False, False, 0)
        body.pack_start(label('API token secret • leave blank to keep the saved secret', 'subtitle'), False, False, 0)
        secret = Gtk.Entry()
        secret.set_visibility(False)
        secret.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        body.pack_start(secret, False, False, 0)
        error_label = label('', 'error')
        body.pack_start(error_label, False, False, 0)
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            if self.demo:
                error_label.set_text('Preview mode does not save settings.')
                continue
            try:
                values = {k:e.get_text() for k,e in fields.items()}
                token = secret.get_text().strip() or core.load_secret()
                self.settings = core.save_settings(values, token)
                dialog.destroy()
                self.refresh()
                return
            except (ValueError, core.LauncherError, OSError) as e:
                error_label.set_text(str(e))
        dialog.destroy()

if __name__ == '__main__':
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    window = Window(demo='--demo' in sys.argv)
    Gtk.main()
