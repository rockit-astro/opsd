#
# This file is part of the Robotic Observatory Control Kit (rockit)
#
# rockit is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# rockit is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with rockit.  If not, see <http://www.gnu.org/licenses/>.

"""Interface to allow the dome controller to operate SuperWASP's roof"""

from rockit.operations.constants import CommandStatus, DomeStatus
from rockit.common import daemons, validation
from rockit.roof import RoofStatus, HeartbeatStatus, CommandStatus as RoofCommandStatus

CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': ['module'],
    'required': [
        'daemon', 'heartbeat_timeout',
    ],
    'properties': {
        'daemon': {
            'type': 'string',
            'daemon_name': True
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
    """Interface to allow the dome controller to operate an Astrohaven dome via domed"""

    def __init__(self, dome_config_json):
        self._daemon = getattr(daemons, dome_config_json['daemon'])

        # Timeout period (seconds) for the roof controller
        # The dome heartbeat is pinged once per LOOP_DELAY when the roof is under
        # automatic control and is fully open or fully closed.
        self._heartbeat_timeout = dome_config_json['heartbeat_timeout']

        self._reopen_after_weather_alert = dome_config_json['reopen_after_weather_alert']
        self._environment_stale_limit = dome_config_json['environment_stale_limit']

    def query_status(self):
        try:
            with self._daemon.connect() as roof:
                status = roof.status()
        except:
            return DomeStatus.Offline

        if status.get('heartbeat_status', None) == HeartbeatStatus.TimedOut:
            return DomeStatus.Timeout

        if status['status'] == RoofStatus.Open:
            return DomeStatus.Open

        if status['status'] == RoofStatus.Closed:
            return DomeStatus.Closed

        if status['status'] == RoofStatus.Opening:
            return DomeStatus.Opening

        if status['status'] == RoofStatus.Closing:
            return DomeStatus.Closing

        return DomeStatus.Offline


    def set_automatic(self):
        try:
            with self._daemon.connect() as roof:
                status = roof.status()
                if status is None:
                    print('roof: roof is not initialized')
                    return CommandStatus.DomeNotInitialized

                if status['heartbeat_status'] == HeartbeatStatus.TimedOut:
                    print('roof: dome heartbeat has tripped')
                    return CommandStatus.DomeHeartbeatTripped

                print('roof: sending initial heartbeat ping')
                if roof.set_heartbeat_timer(self._heartbeat_timeout) != RoofCommandStatus.Succeeded:
                    return CommandStatus.Failed

                return CommandStatus.Succeeded
        except:
            print('roof: exception when setting roof to automatic')
            return CommandStatus.Failed

    def set_manual(self):
        try:
            with self._daemon.connect() as roof:
                status = roof.status()
                if status.get('heartbeat_status', None) != HeartbeatStatus.Active:
                    return CommandStatus.Succeeded

                print('roof: disabling heartbeat')
                if roof.set_heartbeat_timer(0) != RoofCommandStatus.Succeeded:
                    return CommandStatus.Failed

                return CommandStatus.Succeeded
        except:
            print('roof: exception when setting roof to manual')
            return CommandStatus.Failed

    def ping_heartbeat(self):
        try:
            with self._daemon.connect() as roof:
                if roof.set_heartbeat_timer(self._heartbeat_timeout) != RoofCommandStatus.Succeeded:
                    return CommandStatus.Failed
        except:
            print('roof: exception when pinging heartbeat')
            return CommandStatus.Failed

    def close(self):
        try:
            with self._daemon.connect() as roof:
                print('roof: closing')
                ret = roof.close(blocking=False, override=True)
            return ret == RoofCommandStatus.Succeeded
        except:
            print('close: exception when closing')
            return False

    def open(self):
        try:
            with self._daemon.connect() as roof:
                print('roof: opening')
                ret = roof.open(blocking=False, override=True)

            return ret == RoofCommandStatus.Succeeded
        except:
            print('roof: exception when opening')
            return False

    @property
    def reopen_after_weather_alert(self):
        return self._reopen_after_weather_alert

    @property
    def environment_stale_limit(self):
        return self._environment_stale_limit
