#!/usr/bin/env python3
"""fanmon: fan mode and temperature monitor (GTK4 + libadwaita).

Opens a Unix socket to the systemd-activated Rust sampler while the window is
open. The sampler streams newline-delimited JSON snapshots; closing the window
closes the socket and lets the sampler exit. The GUI shows a setup page with an
"Install/Repair Sampler…" button if the socket is unavailable; pkexec is used
only for the one-time system installation, not normal launches.
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

SOCKET_PATH = '/run/fanmon.sock'
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
RETRY_SECONDS = 1
STALE_CHECK_SECONDS = 1
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

        self.install_button = Gtk.Button(label='Install/Repair Sampler…',
                                         halign=Gtk.Align.CENTER)
        self.install_button.add_css_class('suggested-action')
        self.install_button.connect('clicked', self.on_install_clicked)
        self.install_button.set_visible(
            bool(shutil.which('pkexec')) and os.path.exists(INSTALL_SCRIPT))

        self.status_page = Adw.StatusPage(
            icon_name='dialog-warning-symbolic',
            title='Fan data unavailable',
            description='The on-demand root sampler runs only while Fan Monitor is open.\n\n'
                        'Initial system setup requires authentication once.\n\n'
                        'Install and enable the socket with:\n' + INSTALL_HINT,
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
        self._connection = None
        self._input_stream = None
        self._output_stream = None
        self._cancellable = None
        self._connecting = False
        self._closed = False
        self._retry_timeout = None
        self._last_snapshot_ts = None
        self._stream_error = False
        self._stale_timeout = GLib.timeout_add_seconds(
            STALE_CHECK_SECONDS, self.refresh_staleness)
        self.connect('close-request', self.on_close_request)
        self._connect()

    def on_close_request(self, *_args):
        self._closed = True
        if self._stale_timeout:
            GLib.source_remove(self._stale_timeout)
            self._stale_timeout = None
        if self._retry_timeout:
            GLib.source_remove(self._retry_timeout)
            self._retry_timeout = None
        self._release_connection()
        return False

    def _connect(self):
        if self._closed or self._connecting or self._connection is not None:
            return
        self._connecting = True
        self._stream_error = False
        self._cancellable = Gio.Cancellable()
        client = Gio.SocketClient.new()
        try:
            client.connect_async(
                Gio.UnixSocketAddress.new(SOCKET_PATH),
                self._cancellable, self._on_connected, None)
        except (GLib.Error, TypeError):
            self._connecting = False
            self._cancellable = None
            self._show_unavailable()
            self._schedule_retry()

    def _on_connected(self, client, result, _user_data=None):
        try:
            connection = client.connect_finish(result)
        except GLib.Error:
            if not self._closed:
                self._connecting = False
                self._cancellable = None
                self._show_unavailable()
                self._schedule_retry()
            return

        if self._closed:
            connection.close(None)
            return
        self._connecting = False
        self._connection = connection
        self._input_stream = Gio.DataInputStream.new(
            connection.get_input_stream())
        self._output_stream = connection.get_output_stream()
        try:
            self._output_stream.write_all_async(
                b'START\n', GLib.PRIORITY_DEFAULT, self._cancellable,
                self._on_started, None)
        except Exception as err:
            self._end_connection(f'Could not start sampler: {err}')

    def _on_started(self, stream, result, _user_data=None):
        if self._closed or self._connection is None:
            return
        try:
            write_result = stream.write_all_finish(result)
            success = (write_result[0] if isinstance(write_result, tuple)
                       else write_result)
        except Exception as err:
            self._end_connection(f'Could not start sampler: {err}')
            return
        if not success:
            self._end_connection('Could not start sampler.')
            return
        self._read_next_line()

    def _read_next_line(self):
        if self._closed or self._connection is None:
            return
        try:
            self._input_stream.read_line_async(
                GLib.PRIORITY_DEFAULT, self._cancellable,
                self._on_line_read, None)
        except Exception as err:
            self._end_connection(f'Could not read sampler data: {err}')

    def _on_line_read(self, stream, result, _user_data=None):
        if self._closed or self._connection is None:
            return
        try:
            read_result = stream.read_line_finish_utf8(result)
            line = (read_result[0] if isinstance(read_result, tuple)
                    else read_result)
        except Exception as err:
            self._end_connection(f'Could not read sampler data: {err}')
            return

        if line is None:
            self._end_connection()
            return

        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError('snapshot is not a JSON object')
            if 'error' in data:
                self._stream_error = True
                message = str(data['error'])
                self.show_status('Sampler error', 'Sampler error', message)
            else:
                self._render_snapshot(data)
        except Exception as err:
            self._end_connection(f'Invalid sampler data: {err}')
            return
        self._read_next_line()

    def _render_snapshot(self, data):
        fan = data['fan']
        timestamp = data['ts']
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise ValueError('snapshot timestamp is not numeric')
        stamp = time.strftime('%H:%M:%S', time.localtime(timestamp))
        fan_rows = [
            ('Mode', fmt_mode(fan.get('state'))),
            ('EC temperature', fmt_temp(fan.get('temp'))),
        ]

        temps = [(sensor['name'], fmt_temp(sensor['temp']))
                 for sensor in data.get('cpu', [])]
        for zone in data.get('zones', []):
            # x86_pkg_temp is the same die sensor as the coretemp Package row
            if zone['name'] != 'x86_pkg_temp':
                temps.append((zone['name'], fmt_temp(zone['temp'])))

        ec_rows = [
            ('State register (0x55)', '0x{:02X}'.format(fan['state'])),
            ('Speed register (0x5C:0x5D)',
             '0x{:02X}{:02X}'.format(fan.get('speed_hi', 0),
                                     fan.get('speed_lo', 0))),
        ]
        self.sections['fan'].update(fan_rows)
        self.sections['temperature'].update(temps)
        self.sections['ec'].update(ec_rows)
        self._last_snapshot_ts = timestamp
        self.stack.set_visible_child_name('content')
        self.refresh_staleness()

    def refresh_staleness(self):
        if self._closed:
            return GLib.SOURCE_REMOVE
        if (self._last_snapshot_ts is not None
                and self.stack.get_visible_child_name() == 'content'):
            age = time.time() - self._last_snapshot_ts
            stamp = time.strftime(
                '%H:%M:%S', time.localtime(self._last_snapshot_ts))
            if age > STALE_AFTER:
                self.window_title.set_subtitle(
                    f'Stale — last update {stamp}')
            else:
                self.window_title.set_subtitle(f'Updated {stamp}')
        return GLib.SOURCE_CONTINUE

    def _show_unavailable(self):
        if os.path.exists(SAMPLER_BIN):
            self.show_status(
                'Sampler socket unavailable', 'Sampler socket unavailable',
                'The on-demand sampler socket is unavailable.\n\n'
                'Click Install/Repair to enable it, or run:\n' + INSTALL_HINT)
        else:
            self.show_status(
                'Sampler not installed', 'Sampler not installed',
                'The Rust sampler and socket are not installed.\n\n'
                'Click Install/Repair and authenticate once, or run:\n' + INSTALL_HINT)

    def _schedule_retry(self):
        if self._closed or self._retry_timeout is not None:
            return
        self._retry_timeout = GLib.timeout_add_seconds(
            RETRY_SECONDS, self._retry_connection)

    def _retry_connection(self):
        self._retry_timeout = None
        self._connect()
        return GLib.SOURCE_REMOVE

    def _retry_now(self):
        if self._retry_timeout:
            GLib.source_remove(self._retry_timeout)
            self._retry_timeout = None
        self._connect()

    def _release_connection(self):
        cancellable, self._cancellable = self._cancellable, None
        connection, self._connection = self._connection, None
        self._input_stream = None
        self._output_stream = None
        self._connecting = False
        if cancellable is not None:
            cancellable.cancel()
        if connection is not None:
            try:
                connection.close(None)
            except GLib.Error:
                pass

    def _end_connection(self, error=None):
        had_stream_error = self._stream_error
        self._release_connection()
        if self._closed:
            return
        if error is not None:
            self.show_status('Sampler error', 'Sampler error', str(error))
        elif not had_stream_error:
            self._show_unavailable()
        self._schedule_retry()

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
            pass  # cancelled or failed; the connection retry shows status
        self._install_proc = None
        self.install_button.set_sensitive(True)
        self.install_button.set_label('Install/Repair Sampler…')
        if self._connection is None and not self._connecting:
            self._show_unavailable()
        self._retry_now()

    def show_status(self, subtitle, title, description):
        self.status_page.set_title(title)
        self.status_page.set_description(description)
        self.stack.set_visible_child_name('status')
        self.window_title.set_subtitle(subtitle)


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
