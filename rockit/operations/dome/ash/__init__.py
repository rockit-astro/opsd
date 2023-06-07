#
# This file is part of opsd.
#
# opsd is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# opsd is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with opsd.  If not, see <http://www.gnu.org/licenses/>.

"""Interface to allow the dome controller to operate an Ash dome via ashdomed"""

from warwick.observatory.ashdome import (
    CommandStatus as DomeCommandStatus,
    AzimuthStatus, ShutterStatus, HeartbeatStatus)
from warwick.observatory.operations.constants import CommandStatus, DomeStatus
from warwick.observatory.common import daemons, validation

CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': ['module'],
    'required': [
        'daemon', 'init_timeout', 'heartbeat_timeout', 'reopen_after_weather_alert', 'environment_stale_limit'
    ],
    'properties': {
        'daemon': {
            'type': 'string',
            'daemon_name': True
        },
        'init_timeout': {
            'type': 'number',
            'minimum': 0
        },
        'heartbeat_timeout': {
            'type': 'number',
            'minimum': 0
        },
        'reopen_after_weather_alert': {
            'type': 'boolean'
        },
        'environment_stale_limit': {
            'type': 'number',
            'minimum': 0
        }
    }
}


def validate_config(config_json):
    return validation.validation_errors(config_json, CONFIG_SCHEMA, {
        'daemon_name': validation.daemon_name_validator,
    })


class DomeInterface:
    """Interface to allow the dome controller to operate an ash dome via ashdomed"""

    def __init__(self, dome_config_json):
        self._daemon = getattr(daemons, dome_config_json['daemon'])

        self._init_timeout = dome_config_json['init_timeout']

        # Timeout period (seconds) for the dome controller
        # The dome heartbeat is pinged once per LOOP_DELAY when the dome is under
        # automatic control.
        self._heartbeat_timeout = dome_config_json['heartbeat_timeout']

        self._reopen_after_weather_alert = dome_config_json['reopen_after_weather_alert']
        self._environment_stale_limit = dome_config_json['environment_stale_limit']

    def query_status(self):
        try:
            with self._daemon.connect() as dome:
                status = dome.status()
        except:
            return DomeStatus.Offline

        if status.get('heartbeat_status', None) in [HeartbeatStatus.TrippedClosing, HeartbeatStatus.TrippedIdle]:
            return DomeStatus.Timeout

        if status['shutter'] == ShutterStatus.Open:
            return DomeStatus.Open

        if status['shutter'] == ShutterStatus.Closed:
            return DomeStatus.Closed

        if status['shutter'] == ShutterStatus.Opening:
            return DomeStatus.Opening

        if status['shutter'] == ShutterStatus.Closing:
            return DomeStatus.Closing

        return DomeStatus.Offline

    def set_automatic(self):
        try:
            with self._daemon.connect() as dome:
                status = dome.status()
                if status['shutter'] == ShutterStatus.Disconnected:
                    print('dome: dome is not initialized')
                    return CommandStatus.DomeNotInitialized

                if status['azimuth_status'] == AzimuthStatus.NotHomed:
                    print('dome: dome has not been homed')
                    return CommandStatus.DomeNotInitialized

                if status['heartbeat_status'] not in [HeartbeatStatus.Disabled, HeartbeatStatus.Active]:
                    print('dome: dome heartbeat has tripped')
                    return CommandStatus.DomeHeartbeatTripped

                print('dome: sending initial heartbeat ping')
                if dome.set_heartbeat_timer(self._heartbeat_timeout) != DomeCommandStatus.Succeeded:
                    return CommandStatus.Failed

                return CommandStatus.Succeeded
        except:
            print('dome: exception when setting dome to automatic')
            return CommandStatus.Failed

    def set_manual(self):
        try:
            with self._daemon.connect() as dome:
                status = dome.status()
                if status.get('heartbeat_status', None) != HeartbeatStatus.Active:
                    return CommandStatus.Succeeded

                print('dome: disabling heartbeat')
                if dome.set_heartbeat_timer(0) != DomeCommandStatus.Succeeded:
                    return CommandStatus.Failed

                return CommandStatus.Succeeded
        except:
            print('dome: exception when setting dome to manual')
            return CommandStatus.Failed

    def ping_heartbeat(self):
        try:
            with self._daemon.connect() as dome:
                if dome.set_heartbeat_timer(self._heartbeat_timeout) != DomeCommandStatus.Succeeded:
                    return CommandStatus.Failed
        except:
            print('dome: exception when pinging heartbeat')
            return CommandStatus.Failed

    def close(self):
        try:
            with self._daemon.connect() as dome:
                print('dome: parking')
                dome.stop_azimuth()
                dome.slew_named('park', blocking=False)

                print('dome: closing')
                ret = dome.close_shutter(blocking=False, override=True)
            return ret == DomeCommandStatus.Succeeded
        except:
            print('dome: exception when parking/closing')
            return False

    def open(self):
        try:
            with self._daemon.connect() as dome:
                print('dome: opening')
                ret = dome.open_shutter(blocking=False, override=True)

            return ret == DomeCommandStatus.Succeeded
        except:
            print('dome: exception when opening')
            return False

    @property
    def reopen_after_weather_alert(self):
        return self._reopen_after_weather_alert

    @property
    def environment_stale_limit(self):
        return self._environment_stale_limit
