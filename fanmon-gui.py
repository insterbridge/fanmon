#!/usr/bin/env python3
"""fanmon: fan mode and temperature monitor (GTK4 + libadwaita).

Reads the JSON snapshot written by the fanmon sampler (/run/fanmon/status.json)
and shows the EC fan mode, the EC control temperature, CPU/ACPI temperatures and
the raw EC fan registers. Layout follows the GNOME/libadwaita style used by the
Powersupply app: a clamped column of heading + boxed-list sections.

If the sampler is not installed or not running, the window shows a status page
with an "Install Sampler…" button that runs the installer via pkexec (one
password prompt); the view switches to live data automatically once the
sampler starts writing.
"""
import json
import os
import shutil
import sys
import time

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

STATUS_FILE = '/run/fanmon/status.json'
SAMPLER_BIN = '/usr/local/libexec/fanmon-sampler'
APP_ID = 'io.github.insterbridge.Fanmon'


def _install_script():
    """install-system.sh: path baked in by install-user.sh, else next to this file."""
    baked = '@FANMON_SRC@'
    if not baked.startswith('@'):
        return os.path.join(baked, 'install-system.sh')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'install-system.sh')


INSTALL_SCRIPT = _install_script()
INSTALL_HINT = 'sudo ' + INSTALL_SCRIPT
REFRESH_SECONDS = 1
STALE_AFTER = 5


def fmt_temp(value):
    return f'{value:.0f} °C' if value is not None else '—'


# Register 0x55 holds the EC fan speed level; on this Acer EC family the scale
# is 0-8 (nbfc's E5-471G config uses register 85/0x55 with MinSpeed 0, MaxSpeed
# 8). Observed here: 2 at idle, 3/4/5 under rising load. The GUI owns the
# labels so they can be adjusted without reinstalling the root sampler.
STATE_NAMES = {0: 'Off', 1: 'Idle', 2: 'Idle', 3: 'Low', 4: 'Medium'}


def fmt_mode(state):
    if state is None:
        return '—'
    if state in STATE_NAMES:
        return STATE_NAMES[state]
    if 5 <= state <= 8:
        return 'High'
    return 'Unknown (0x{:02X})'.format(state)


class Section:
    """Section heading with a boxed list of title/value rows."""

    def __init__(self, title):
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class('heading')
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class('boxed-list')
        self.box.append(heading)
        self.box.append(self.listbox)
        self.rows = {}

    def update(self, pairs):
        wanted = [label for label, _ in pairs]
        for label in list(self.rows):
            if label not in wanted:
                row, _ = self.rows.pop(label)
                self.listbox.remove(row)
        for position, (label, value) in enumerate(pairs):
            if label in self.rows:
                self.rows[label][1].set_text(value)
                continue
            line = Gtk.Box(margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
            title = Gtk.Label(label=label, xalign=0, hexpand=True)
            value_label = Gtk.Label(label=value, css_classes=['dim-label'])
            value_label.set_halign(Gtk.Align.END)
            line.append(title)
            line.append(value_label)
            row = Gtk.ListBoxRow(child=line)
            self.listbox.insert(row, position)
            self.rows[label] = (row, value_label)


class FanmonWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application)
        self.set_title('Fan Monitor')
        self.set_default_size(480, 640)

        self.window_title = Adw.WindowTitle(title='Fan Monitor')
        header = Adw.HeaderBar(title_widget=self.window_title)

        self.install_button = Gtk.Button(label='Install Sampler…',
                                         halign=Gtk.Align.CENTER)
        self.install_button.add_css_class('suggested-action')
        self.install_button.connect('clicked', self.on_install_clicked)
        self.install_button.set_visible(
            bool(shutil.which('pkexec')) and os.path.exists(INSTALL_SCRIPT))

        self.status_page = Adw.StatusPage(
            icon_name='dialog-warning-symbolic',
            title='Fan data unavailable',
            description='The fanmon sampler is not running.\n\n'
                        'Install and start it with:\n' + INSTALL_HINT,
        )
        self.status_page.set_child(self.install_button)

        self.sections = {
            'fan': Section('Fan'),
            'temperature': Section('Temperature'),
            'ec': Section('Embedded controller'),
        }
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        page.set_margin_start(16)
        page.set_margin_end(16)
        page.set_margin_top(16)
        page.set_margin_bottom(16)
        for section in self.sections.values():
            page.append(section.box)

        scrolled = Gtk.ScrolledWindow(child=Adw.Clamp(child=page), vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.stack = Gtk.Stack()
        self.stack.add_named(self.status_page, 'status')
        self.stack.add_named(scrolled, 'content')

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.set_content(toolbar)

        self._install_proc = None
        self._timeout = GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)
        self.connect('close-request', self.on_close_request)
        self.refresh()

    def on_close_request(self, *_args):
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = None
        return False

    def on_install_clicked(self, _button):
        if self._install_proc is not None:
            return
        try:
            proc = Gio.Subprocess.new(['pkexec', INSTALL_SCRIPT],
                                      Gio.SubprocessFlags.NONE)
        except GLib.Error as err:
            self.status_page.set_description(
                'Could not launch the installer:\n' + err.message +
                '\n\nRun it manually:\n' + INSTALL_HINT)
            return
        self._install_proc = proc
        self.install_button.set_sensitive(False)
        self.install_button.set_label('Waiting for authentication…')
        proc.wait_check_async(None, self.on_install_done)

    def on_install_done(self, proc, result):
        try:
            proc.wait_check_finish(result)
        except GLib.Error:
            pass  # cancelled or failed; refresh() re-shows the status page
        self._install_proc = None
        self.install_button.set_sensitive(True)
        self.install_button.set_label('Install Sampler…')
        self.refresh()

    def show_status(self, subtitle, title, description):
        self.status_page.set_title(title)
        self.status_page.set_description(description)
        self.stack.set_visible_child_name('status')
        self.window_title.set_subtitle(subtitle)

    def refresh(self):
        try:
            with open(STATUS_FILE) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            if os.path.exists(SAMPLER_BIN):
                self.show_status(
                    'Sampler not running', 'Sampler not running',
                    'The fanmon sampler service is not running.\n\n'
                    'Click Install to reinstall and start it, or run:\n'
                    'sudo systemctl restart fanmon')
            else:
                self.show_status(
                    'Sampler not installed', 'Sampler not installed',
                    'The background sampler reads the EC fan registers\n'
                    'and needs root once.\n\n'
                    'Click Install and authenticate, or run:\n' + INSTALL_HINT)
            return GLib.SOURCE_CONTINUE

        if 'error' in data:
            self.show_status('Sampler error', 'Sampler error', str(data['error']))
            return GLib.SOURCE_CONTINUE

        fan = data['fan']
        self.sections['fan'].update([
            ('Mode', fmt_mode(fan.get('state'))),
            ('EC temperature', fmt_temp(fan.get('temp'))),
        ])

        temps = [(sensor['name'], fmt_temp(sensor['temp'])) for sensor in data.get('cpu', [])]
        for zone in data.get('zones', []):
            # x86_pkg_temp is the same die sensor as the coretemp Package row
            if zone['name'] != 'x86_pkg_temp':
                temps.append((zone['name'], fmt_temp(zone['temp'])))
        self.sections['temperature'].update(temps)

        self.sections['ec'].update([
            ('State register (0x55)', '0x{:02X}'.format(fan['state'])),
            ('Speed register (0x5C:0x5D)',
             '0x{:02X}{:02X}'.format(fan.get('speed_hi', 0), fan.get('speed_lo', 0))),
        ])

        self.stack.set_visible_child_name('content')
        age = time.time() - data['ts']
        stamp = time.strftime('%H:%M:%S', time.localtime(data['ts']))
        if age > STALE_AFTER:
            self.window_title.set_subtitle(f'Stale — last update {stamp}')
        else:
            self.window_title.set_subtitle(f'Updated {stamp}')
        return GLib.SOURCE_CONTINUE


class FanmonApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        window = self.props.active_window
        if window is None:
            window = FanmonWindow(self)
        window.present()


def main():
    return FanmonApplication().run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
