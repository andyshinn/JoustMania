from multiprocessing import Queue, Manager, Process
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, jsonify)
from wtforms import (Form, SelectField, SelectMultipleField, BooleanField,
                     StringField, PasswordField, widgets, FieldList)
from os import environ
from sys import platform
import common, colors
import json
import yaml
import logging
import runtime_platform
import bluetooth_diagnostics
import network_manager
from system_power import request_system_power

if platform == "linux" or platform == "linux2":
    import bluetooth_roles
    import jm_dbus
    import psmove_dbus
else:
    bluetooth_roles = None
    psmove_dbus = None

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

# The PIN gate protects /network only. 10,000 combinations falls to a script in
# seconds, so the lockout is what actually does the work here.
PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_SECS = 60

# URLs each OS fetches to decide whether a network is "working". Answering
# these with a redirect is what makes a phone pop the setup page by itself.
#
# The response each one expects when the network *is* working matters too: we
# claim these paths unconditionally, and they used to 404. A 404 reads as "a
# portal is intercepting me", which would pop a spurious sign-in browser for
# anyone using the permanent access point from enable_ap.sh. So when our portal
# is off, answer exactly what a working network answers.
_HTML_SUCCESS = ('<HTML><HEAD><TITLE>Success</TITLE></HEAD>'
                 '<BODY>Success</BODY></HTML>')
CAPTIVE_PROBE_RESPONSES = {
    '/generate_204': ('', 204),                             # Android
    '/gen_204': ('', 204),
    '/hotspot-detect.html': (_HTML_SUCCESS, 200),           # iOS, macOS
    '/library/test/success.html': (_HTML_SUCCESS, 200),
    '/ncsi.txt': ('Microsoft NCSI', 200),                   # Windows
    '/connecttest.txt': ('Microsoft Connect Test', 200),
    '/redirect': ('', 204),
    '/canonical.html': (_HTML_SUCCESS, 200),                # Firefox
    '/success.txt': ('success\n', 200),
}
CAPTIVE_PROBE_PATHS = tuple(CAPTIVE_PROBE_RESPONSES)


def web_port():
    default_port = runtime_platform.default_web_port()
    return int(environ.get("JOUSTMANIA_WEB_PORT", default_port))


def web_urls():
    port = web_port()
    suffix = "" if port == 80 else ":{}".format(port)
    urls = ["http://localhost{}".format(suffix)]

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as web_socket:
            web_socket.connect(("8.8.8.8", 80))
            local_address = web_socket.getsockname()[0]
        if not local_address.startswith("127."):
            urls.append("http://{}{}".format(local_address, suffix))
    except OSError:
        pass

    return urls


class MultiCheckboxField(SelectMultipleField):
    """
    A multiple-select, except displays a list of checkboxes.

    Iterating the field will produce subfields, allowing custom rendering of
    the enclosed checkbox fields.
    """
    widget = widgets.ListWidget(prefix_label=True)
    option_widget = widgets.CheckboxInput()


class SettingsForm(Form):
    move_can_be_admin = BooleanField('Allow Move to change settings')
    play_instructions = BooleanField('Play instructions before game start')
    play_audio = BooleanField('Play audio')
    red_on_kill = SelectField('Kill notification',choices=[(True,'Red'),('','Dark')],coerce=bool)
    sensitivity = SelectField('Move sensitivity',choices=[(0,'Ultra High'),(1,'High'),(2,'Medium'),(3,'Low'),(4,'Ultra Low')],coerce=int)
    mode_selection = SelectField('Mode selection', choices=[game.pretty_name for game in common.Games],coerce=str)   
    mode_options = [ game for game in common.Games if game not in [common.Games.Random, common.Games.JoustTeams]]
    random_modes = MultiCheckboxField('Random Modes',choices=[(game.name, game.pretty_name) for game in mode_options])
    color_lock = BooleanField('Lock team colors')
    color_choices = [(color.name,color.name) for color in colors.team_color_list]
    color_lock_choices = FieldList(SelectField('',choices=color_choices,coerce=str),min_entries=9)
    random_teams = BooleanField('Randomize teams each round')
    force_all_start = BooleanField('When force starting start with all or only those who pushed trigger')
    random_team_size = SelectField('size of random teams',choices=[(2,'2'),(3,'3'),(4,'4'),(5,'5'),(6,'6')],coerce=int)

    # LCD KeyPad HAT (DFR0514) and UPS HAT (DFR0494). These are SelectFields
    # rather than IntegerFields so the ranges are enforced for free and a blank
    # input can never post None over a saved value.
    _enable_choices = [('auto','Auto-detect'),('on','Always on'),('off','Off')]
    _percent_choices = [(pct,'{}%'.format(pct)) for pct in range(0,101,5)]
    lcd_enabled = SelectField('LCD HAT',choices=_enable_choices,coerce=str)
    lcd_brightness = SelectField('LCD brightness',
                                 choices=[c for c in _percent_choices if c[0] >= 10],coerce=int)
    lcd_idle_brightness = SelectField('LCD brightness when idle',
                                      choices=_percent_choices,coerce=int)
    lcd_idle_dim_secs = SelectField('Dim the LCD after',
                                    choices=[(0,'Never'),(30,'30 seconds'),(60,'1 minute'),
                                             (120,'2 minutes'),(300,'5 minutes'),
                                             (600,'10 minutes'),(900,'15 minutes')],coerce=int)
    lcd_backlight_ambient = BooleanField('Tint the LCD backlight by game state')
    ups_enabled = SelectField('UPS HAT',choices=_enable_choices,coerce=str)
    ups_warn_percent = SelectField('Warn when battery below',
                                   choices=[(pct,'{}%'.format(pct)) for pct in range(5,51,5)],coerce=int)
    ups_critical_percent = SelectField('Shut down when battery below',
                                       choices=[(pct,'{}%'.format(pct)) for pct in range(1,26)],coerce=int)
    ups_auto_shutdown = BooleanField('Shut down automatically on critical battery')

    # Captive portal. SSID/password are free text because they are genuinely
    # arbitrary; everything else stays a SelectField per the note above.
    portal_ssid = StringField('Setup access point name')
    portal_password = StringField('Setup access point password')
    portal_auto_fallback = BooleanField(
        'Start the setup access point automatically when there is no network')
    portal_fallback_delay_secs = SelectField(
        'Wait this long before starting it',
        choices=[(30,'30 seconds'),(60,'1 minute'),(90,'90 seconds'),
                 (120,'2 minutes'),(300,'5 minutes'),(600,'10 minutes')],
        coerce=int)


class NetworkPinForm(Form):
    pin = PasswordField('PIN')


class WifiForm(Form):
    """SSID is a select populated from a live scan, with a text box beside it
    for hidden networks -- picking from a list is far less error-prone on a
    phone than retyping an SSID."""
    ssid = SelectField('Network', choices=[], coerce=str)
    hidden_ssid = StringField('Or a hidden network')
    password = PasswordField('Password')


class EthernetForm(Form):
    method = SelectField('Wired addressing',
                         choices=[('auto', 'Automatic (DHCP)'),
                                  ('manual', 'Static address')], coerce=str)
    address = StringField('Address with prefix, e.g. 192.168.1.50/24')
    gateway = StringField('Gateway')
    dns = StringField('DNS server')


class WebUI():
    def __init__(self, command_queue=Queue(), ns=None, controller_manager_instance=None):

        self.app = Flask(__name__)
        # Was a hardcoded string, which is published in this repo -- anyone
        # could forge a signed session cookie and walk straight past the PIN
        # gate below. The only cost of randomising it is that flash() messages
        # do not survive a restart.
        self.app.secret_key = os.urandom(32)
        self._pin_attempts = 0
        self._pin_locked_until = 0.0
        self._portal_checked_at = 0.0
        self._portal_active = False
        self.command_queue = command_queue
        self.controller_manager = controller_manager_instance
        if ns == None:

            self.ns = Manager().Namespace()
            self.ns.status = dict()
            self.ns.settings = {
                'sensitivity':1, 
                'red_on_kill':False,
                'random_team_size':3,
                'force_all_start':False,
                'color_lock_choices':{
                    2: ['Magenta','Green'],
                    3: ['Orange','Turquoise','Purple'],
                    4: ['Yellow','Green','Blue','Purple']
            }}
            self.ns.battery_status = dict()
        else:
            self.ns = ns

        self.app.add_url_rule('/','index',self.index)
        self.app.add_url_rule('/changemodestr', 'change_mode_str', self.change_mode_str, methods=['POST'])
        self.app.add_url_rule('/startgame','start_game',self.start_game)
        self.app.add_url_rule('/killgame','kill_game',self.kill_game)
        self.app.add_url_rule('/updateStatus','update',self.update)
        self.app.add_url_rule('/battery','battery_status',self.battery_status)
        self.app.add_url_rule('/debug','debug',self.controller_debug)
        self.app.add_url_rule('/debug/controllers','controller_debug_legacy',self.controller_debug_legacy)
        self.app.add_url_rule('/debug/data','debug_data',self.debug_data)
        self.app.add_url_rule(
            '/debug/reset-bluetooth',
            'reset_bluetooth',
            self.reset_bluetooth,
            methods=['POST'],
        )
        self.app.add_url_rule(
            '/debug/restart-joustmania',
            'restart_joustmania',
            self.restart_joustmania,
            methods=['POST'],
        )
        self.app.add_url_rule('/settings','settings',self.settings, methods=['GET','POST'])
        self.app.add_url_rule('/rand<num_teams>','randomize',self.randomize_teams)
        self.app.add_url_rule('/power','power',self.power)
        self.app.add_url_rule('/reboot8675309','reboot',self.reboot)
        self.app.add_url_rule('/shutdown8675309','shutdown',self.shutdown)
        self.app.add_url_rule('/shutdown','shutdown_lastscreen',self.shutdown_lastscreen)
        self.app.add_url_rule('/network','network',self.network, methods=['GET','POST'])
        self.app.add_url_rule('/network/scan','network_scan',self.network_scan)
        self.app.add_url_rule('/network/wifi','network_wifi',self.network_wifi, methods=['POST'])
        self.app.add_url_rule('/network/forget','network_forget',self.network_forget, methods=['POST'])
        self.app.add_url_rule('/network/ethernet','network_ethernet',self.network_ethernet, methods=['POST'])
        self.app.add_url_rule('/network/portal','network_portal',self.network_portal, methods=['POST'])

        # The captive-portal pieces. Both are no-ops unless the portal is up,
        # so nothing about normal LAN operation changes.
        for path in CAPTIVE_PROBE_PATHS:
            self.app.add_url_rule(path, 'probe_' + path.strip('/').replace('.', '_'),
                                  self.captive_probe)
        self.app.before_request(self.captive_redirect)


    def web_loop(self):
        print("To view the Web UI, go to " + " or ".join(web_urls()), flush=True)
        self.app.run(host='0.0.0.0', port=web_port(), debug=False)

    def web_loop_with_debug(self):
        print("To view the Web UI, go to " + " or ".join(web_urls()), flush=True)
        self.app.run(host='0.0.0.0', port=web_port(), debug=True)

    #@app.route('/')
    def index(self):
        form = SettingsForm()
        return render_template('joustmania.html', form=form)
        #return render_template('joustmania.html')

    #@app.route('/updateStatus')
    def update(self):
        return json.dumps(self.ns.status)
        
        
    #@app.route('/changemodestr')
    def change_mode_str(self):
        mode_name = request.form.get('mode_selection')
        if mode_name:
            self.command_queue.put({'command': 'changemodestr_' + mode_name})
            return "{'status': 'OK'}"
        else:
            return "{'status': 'Error', 'message': 'No mode selected'}"


    #@app.route('/startgame')
    def start_game(self):
        self.command_queue.put({'command': 'startgame'})
        return "{'status':'OK'}"

    #@app.route('/killgame')
    def kill_game(self):
        self.command_queue.put({'command': 'killgame'})
        return "{'status':'OK'}"

    #@app.route('/battery')
    def battery_status(self):
        return self.controller_debug()

    def controller_debug_legacy(self):
        return redirect(url_for('debug'))

    def debug_data(self):
        return {
            "adapters": bluetooth_diagnostics.get_adapters(),
            "controllers": self._controller_debug_data(),
            "ups": self._ups_debug_data(),
        }

    def _ups_debug_data(self):
        """UPS HAT state, published by the LCD process. Empty when absent."""
        try:
            return dict(self.ns.ups_status or {})
        except Exception:
            return {}

    def reset_bluetooth(self):
        """Launch the existing reset workflow after this response is sent.

        The workflow deliberately stops this WebUI along with the game, clears
        saved PS Move registrations, and starts JoustMania again. A detached,
        delayed process lets the browser receive the confirmation page first.
        """
        reset_script = Path(__file__).resolve().parent / 'reset_psmove_connections.sh'
        if not reset_script.is_file():
            return 'Bluetooth reset script was not found.', 500
        # Do not inherit Supervisor's stdout pipe: stopping JoustMania closes
        # that pipe and would kill clear_devices.py with BrokenPipeError.
        reset_log = open('/var/log/joustmania-bluetooth-reset.log', 'a')
        try:
            subprocess.Popen(
                ['/bin/bash', '-c', 'sleep 1; exec "$0"', str(reset_script)],
                stdin=subprocess.DEVNULL,
                stdout=reset_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            reset_log.close()
        return render_template('bluetooth_reset.html')

    def restart_joustmania(self):
        """Restart only the Supervisor-managed JoustMania application."""
        restart_script = Path(__file__).resolve().parent / 'restart_joustmania.sh'
        if not restart_script.is_file():
            return 'JoustMania restart script was not found.', 500
        restart_log = open('/var/log/joustmania-restart.log', 'a')
        try:
            subprocess.Popen(
                ['/bin/bash', '-c', 'sleep 1; exec "$0"', str(restart_script)],
                stdin=subprocess.DEVNULL,
                stdout=restart_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            restart_log.close()
        return render_template('joustmania_restart.html')

    def _controller_debug_data(self):
        battery_status = {
            str(address).upper(): level
            for address, level in dict(self.ns.battery_status).items()
        }
        out_moves = {
            str(address).upper(): value
            for address, value in dict(getattr(self.ns, 'out_moves', {})).items()
        }
        update_counts = {
            str(address).upper(): int(value)
            for address, value in dict(
                getattr(self.ns, 'controller_update_counts', {})
            ).items()
        }
        if self.controller_manager is not None:
            update_counts = {
                str(self.controller_manager.index_to_serial[index]).upper(): int(
                    self.controller_manager.state_sequence[index] // 2
                )
                for index in self.controller_manager.active_controller_indices()
            }

        if psmove_dbus is not None:
            controllers = psmove_dbus.get_registered_controllers()
        else:
            # Windows has no BlueZ/D-Bus registry. Controllers reported by the
            # game are already paired, loaded, and connected through psmoveapi.
            controllers = [
                {
                    'adapter': 'Windows Bluetooth',
                    'adapter_address': '',
                    'address': address,
                    'model': 'Unknown',
                    'registered': True,
                    'loaded': True,
                    'paired': True,
                    'connected': True,
                    'trusted': True,
                    'services_resolved': True,
                }
                for address in battery_status
            ]

        # One controller can retain registrations under multiple adapter
        # addresses after dongles are swapped. Display the physical controller
        # once, preferring its current live/connected BlueZ object over saved
        # registrations belonging to unavailable adapters.
        controllers_by_address = {}
        for controller in controllers:
            address = controller['address'].upper()
            existing = controllers_by_address.get(address)
            rank = (
                bool(controller['connected']),
                bool(controller['loaded']),
                controller['adapter'] != 'unknown',
            )
            if existing is None or rank > existing[0]:
                controllers_by_address[address] = (rank, controller)
        controllers = [item[1] for item in controllers_by_address.values()]

        for controller in controllers:
            address = controller['address'].upper()
            battery = battery_status.get(address)
            controller['status'] = (
                'Connected' if controller['connected']
                else 'Paired, not connected'
            )
            controller['battery'] = common.battery_levels.get(battery, 'Unknown')
            controller['battery_code'] = battery
            controller['active'] = (
                None if address not in out_moves else out_moves[address] == 0
            )
            controller['update_count'] = update_counts.get(address)

        if bluetooth_roles is not None:
            try:
                roles = bluetooth_roles.get_connection_roles(
                    list(jm_dbus.get_hci_dict().keys())
                )
            except Exception:
                roles = {}
            for controller in controllers:
                connection = roles.get(controller['address'].upper(), {})
                controller['role'] = connection.get('role', 'Unavailable')
                controller['handle'] = connection.get('handle')
        else:
            for controller in controllers:
                controller['role'] = 'Unavailable'
                controller['handle'] = None

        controllers.sort(
            key=lambda controller: (
                not controller['connected'],
                controller['adapter'],
                controller['address'],
            )
        )
        return controllers

    def controller_debug(self):
        controllers = self._controller_debug_data()
        adapters = bluetooth_diagnostics.get_adapters()
        for adapter in adapters:
            adapter['controllers'] = [
                controller for controller in controllers
                if controller['adapter'] == adapter['name']
            ]
        return render_template(
            'controller_debug.html',
            controllers=controllers,
            adapters=adapters,
        )

    #@app.route('/power')
    def power(self):
        return render_template('power.html')

    #@app.route('/shutdown8675309')
    def shutdown(self):
        Process(target=request_system_power, args=('poweroff',)).start()
        #use redirect to conceal the url for tripping the shutdown
        return redirect(url_for('shutdown_lastscreen'))

    #@app.route('/shutdown_lastscreen')
    def shutdown_lastscreen(self):
        return render_template('shutdown.html')

    #@app.route('/reboot8675309')
    def reboot(self):
        Process(target=request_system_power, args=('reboot',)).start()
        return redirect(url_for('index'))
        

    #@app.route('/settings')
    def settings(self):
        if request.method == 'POST':
            new_settings = SettingsForm(request.form).data
            self.web_settings_update(new_settings)
            return redirect(url_for('settings'))
        else:
            temp_colors = self.ns.settings['color_lock_choices']
            temp_colors = temp_colors[2] + temp_colors[3] + temp_colors[4]
            settingsForm = SettingsForm(
                sensitivity = self.ns.settings['sensitivity'],
                red_on_kill = self.ns.settings['red_on_kill'],
                random_team_size = self.ns.settings['random_team_size'],
                force_all_start = self.ns.settings['force_all_start'],
                color_lock_choices = temp_colors,
                lcd_enabled = self.ns.settings.get('lcd_enabled', 'auto'),
                lcd_brightness = self.ns.settings.get('lcd_brightness', 100),
                lcd_idle_brightness = self.ns.settings.get('lcd_idle_brightness', 15),
                lcd_idle_dim_secs = self.ns.settings.get('lcd_idle_dim_secs', 120),
                ups_enabled = self.ns.settings.get('ups_enabled', 'auto'),
                ups_warn_percent = self.ns.settings.get('ups_warn_percent', 20),
                ups_critical_percent = self.ns.settings.get('ups_critical_percent', 5),
                portal_ssid = self.ns.settings.get(
                    'portal_ssid', network_manager.DEFAULT_PORTAL_SSID),
                portal_password = self.ns.settings.get(
                    'portal_password', network_manager.DEFAULT_PORTAL_PASSWORD),
                portal_fallback_delay_secs = self.ns.settings.get(
                    'portal_fallback_delay_secs', 90),
            )
            return render_template('settings.html', form=settingsForm, settings=self.ns.settings)

    def web_settings_update(self,web_settings):
        colors_are_good = True
        temp_colors = {
            2: web_settings['color_lock_choices'][0:2],
            3: web_settings['color_lock_choices'][2:5],
            4: web_settings['color_lock_choices'][5:9],
        }
        for key in temp_colors.keys():
            colorset = temp_colors[key]
            if len(colorset) != len(set(colorset)):
                temp_colors[key] = self.ns.settings['color_lock_choices'][key]
                colors_are_good = False

        # Drop Nones so a field the form did not post can never clobber a
        # saved value.
        web_settings = {k: v for k, v in web_settings.items() if v is not None}

        temp_settings = self.ns.settings
        temp_settings.update(web_settings)
        temp_settings['color_lock_choices'] = temp_colors

        # The dim level is meaningless above the normal level.
        temp_settings['lcd_idle_brightness'] = min(
            temp_settings.get('lcd_idle_brightness', 15),
            temp_settings.get('lcd_brightness', 100))

        #secret setting, keep it True
        #temp_settings['enforce_minimum'] = 'enforce_minimum' in web_settings.keys()
        if temp_settings['random_modes'] == []:
            temp_settings['random_modes'] = [common.Games.JoustFFA.name]

        self.ns.settings = temp_settings

        with open(common.SETTINGSFILE,'w') as yaml_file:
            yaml.dump(self.ns.settings,yaml_file)

        if colors_are_good:
            flash('Settings updated!')
        else:
            flash('Duplicate color lock colors! Other settings saved.')

    # -- captive portal ----------------------------------------------------

    def portal_is_active(self):
        """Cached portal check.

        before_request runs on every request, and shelling out to nmcli each
        time would make the whole UI crawl.
        """
        now = time.time()
        if now - self._portal_checked_at > 2.0:
            self._portal_checked_at = now
            self._portal_active = network_manager.portal_active()
        return self._portal_active

    def captive_probe(self):
        """Answer an OS connectivity probe.

        With the portal up, redirect: seeing anything other than the expected
        success response is what makes the phone decide it is behind a portal
        and open the page. With the portal down, answer normally so we do not
        claim to be a portal we are not.
        """
        if self.portal_is_active():
            return redirect(self.portal_url(), code=302)
        return CAPTIVE_PROBE_RESPONSES.get(request.path, ('', 204))

    def portal_url(self):
        port = web_port()
        suffix = "" if port == 80 else ":{}".format(port)
        return "http://{}{}/network".format(network_manager.PORTAL_HOSTNAME, suffix)

    def captive_redirect(self):
        """Send stray requests to the setup page while the portal is up."""
        if not self.portal_is_active():
            return None
        path = request.path
        if path.startswith('/network') or path.startswith('/static'):
            return None
        if path in CAPTIVE_PROBE_PATHS:
            return None                      # its own handler deals with it
        return redirect(self.portal_url(), code=302)

    # -- network settings --------------------------------------------------

    def network_pin(self):
        try:
            return self.ns.network_pin
        except Exception:
            return None

    def pin_required(self):
        """Gate for /network only. Returns a response, or None to continue.

        The PIN is shown on the LCD, so physical access to the machine is the
        credential. That is a real control precisely because it is not
        reachable over the network.
        """
        pin = self.network_pin()
        if not pin:
            # Nothing to check against (standalone WebUI, or piparty never set
            # one). Failing open here beats locking someone out of their Pi.
            return None
        if session.get('network_authed'):
            return None

        error = None
        if time.time() < self._pin_locked_until:
            error = 'Too many attempts. Try again in a minute.'
        elif request.method == 'POST' and 'pin' in request.form:
            submitted = NetworkPinForm(request.form).data['pin'] or ''
            if secrets.compare_digest(str(submitted), str(pin)):
                session['network_authed'] = True
                self._pin_attempts = 0
                return None
            self._pin_attempts += 1
            if self._pin_attempts >= PIN_MAX_ATTEMPTS:
                self._pin_locked_until = time.time() + PIN_LOCKOUT_SECS
                self._pin_attempts = 0
                # Rotating on lockout means a captured PIN also stops working.
                self.rotate_pin()
                error = 'Too many attempts. A new PIN is on the display.'
            else:
                error = 'That PIN was not correct.'

        return render_template('network_pin.html',
                               form=NetworkPinForm(), error=error), 401

    def rotate_pin(self):
        try:
            new_pin = '{:04d}'.format(secrets.randbelow(10000))
            self.ns.network_pin = new_pin
            logger.warning("Network PIN rotated after failed attempts: %s", new_pin)
        except Exception:
            logger.exception("Could not rotate the network PIN")

    def network(self):
        gate = self.pin_required()
        if gate is not None:
            return gate

        state = network_manager.status()
        ethernet = state.get('ethernet') or {}
        # Show the method actually in force. Defaulting the form to DHCP when
        # a static address is configured would invite someone to "save" the
        # page and silently wipe it.
        method = network_manager.connection_method(ethernet.get('connection'))
        return render_template(
            'network.html',
            state=state,
            saved=network_manager.saved_connections(),
            wifi_form=WifiForm(),
            ethernet_form=EthernetForm(
                method='manual' if method == 'manual' else 'auto'),
            portal_ssid=self.ns.settings.get(
                'portal_ssid', network_manager.DEFAULT_PORTAL_SSID),
            ethernet_connection=ethernet.get('connection'),
        )

    def network_scan(self):
        gate = self.pin_required()
        if gate is not None:
            return gate
        # rescan only on an explicit refresh: a forced sweep takes seconds and
        # briefly disrupts an existing association.
        rescan = request.args.get('rescan') == '1'
        return jsonify({'networks': network_manager.scan(rescan=rescan)})

    def network_wifi(self):
        gate = self.pin_required()
        if gate is not None:
            return gate

        data = WifiForm(request.form).data
        ssid = (data.get('hidden_ssid') or '').strip() or data.get('ssid')
        password = data.get('password')
        if not ssid:
            flash('Pick a network, or type the name of a hidden one.')
            return redirect(url_for('network'))

        # Joining takes the AP down, so the browser loses this connection
        # mid-request. Detach it and answer first, exactly as the Bluetooth
        # reset does, or the user never learns where to reconnect.
        target = network_manager.status().get('primary_ip')
        Process(target=_apply_wifi, args=(ssid, password), daemon=True).start()
        return render_template('network_applying.html',
                               ssid=ssid, previous_ip=target)

    def network_forget(self):
        gate = self.pin_required()
        if gate is not None:
            return gate
        ok, message = network_manager.forget(request.form.get('name', ''))
        flash(message)
        return redirect(url_for('network'))

    def network_ethernet(self):
        gate = self.pin_required()
        if gate is not None:
            return gate
        data = EthernetForm(request.form).data
        ok, message = network_manager.set_ethernet(
            data.get('method'), data.get('address'),
            data.get('gateway'), data.get('dns'))
        flash(message)
        return redirect(url_for('network'))

    def network_portal(self):
        gate = self.pin_required()
        if gate is not None:
            return gate
        command = ('lcd_portal_off' if network_manager.portal_active()
                   else 'lcd_portal_on')
        # Routed through the same queue the LCD uses, so piparty stays the one
        # place that owns starting and stopping the portal.
        self.command_queue.put({'command': command})
        self._portal_checked_at = 0.0            # force a fresh read next time
        flash('Captive portal is starting.' if command == 'lcd_portal_on'
              else 'Captive portal is stopping.')
        return render_template('network_applying.html', ssid=None,
                               previous_ip=None,
                               portal=(command == 'lcd_portal_on'))

    #@app.route('/rand<num_teams>')
    def randomize_teams(self,num_teams):
        if num_teams not in '234':
            return "what are you doing here?"
        else:
            num_teams = int(num_teams)
            team_colors = colors.generate_team_colors(num_teams)
            team_colors = [color.name for color in team_colors]
            return str(team_colors).replace("'",'"')#JSON is dumb and demands double quotes

def _apply_wifi(ssid, password):
    """Join a network after the browser has been answered.

    A short delay lets the response reach the phone before the access point it
    arrived over disappears.
    """
    time.sleep(1)
    ok, message = network_manager.join_wifi(ssid, password)
    logger.info("Wifi join %s: %s", 'succeeded' if ok else 'failed', message)


def start_web(command_queue, ns, controller_manager_instance=None):
    import setproctitle
    setproctitle.setproctitle(f"JoustMania-WebUI")    
    webui = WebUI(command_queue, ns, controller_manager_instance)
    webui.web_loop()

if __name__ == '__main__':
    webui = WebUI()
    webui.web_loop_with_debug()
