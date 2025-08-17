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

"""Interface to allow the dome controller to operate an Ash dome via ash_domed"""

from rockit.dome.ash import (
    CommandStatus as DomeCommandStatus,
    ShutterStatus, HeartbeatStatus)
from rockit.operations.constants import DomeStatus
from rockit.common import daemons, validation

CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': ['module'],
    'required': [
        'daemon', 'movement_timeout', 'heartbeat_timeout'
    ],
    'properties': {
        'daemon': {
            'type': 'string',
            'daemon_name': True
        },
        'movement_timeout': {
            'type': 'number',
            'minimum': 0
        },
        'heartbeat_timeout': {
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
    """Interface to allow the dome controller to operate an ash dome via ash_domed"""

    def __init__(self, dome_config_json):
        self._daemon = getattr(daemons, dome_config_json['daemon'])

        # Communications timeout when opening or closing the dome
        self._movement_timeout = dome_config_json['movement_timeout']

        # Timeout period (seconds) for the dome controller
        # The dome heartbeat is pinged once per LOOP_DELAY when the dome is under
        # automatic control and is fully open or fully closed.  This timeout should
        # be large enough to account for the time it takes to open and close the dome
        self._heartbeat_timeout = dome_config_json['heartbeat_timeout']

    def query_status(self):
        with self._daemon.connect() as dome:
            status = dome.status()

        if status['heartbeat_status'] in [HeartbeatStatus.TrippedClosing, HeartbeatStatus.TrippedIdle]:
            return DomeStatus.Timeout

        if status['shutter'] == ShutterStatus.Closed:
            return DomeStatus.Closed

        if status['shutter'] in [ShutterStatus.Opening, ShutterStatus.Closing]:
            return DomeStatus.Moving

        return DomeStatus.Open

    def ping_heartbeat(self):
        with self._daemon.connect() as dome:
            ret = dome.set_heartbeat_timer(self._heartbeat_timeout)
            return ret == DomeCommandStatus.Succeeded

    def disable_heartbeat(self):
        print('dome: disabling heartbeat')
        with self._daemon.connect() as dome:
            ret = dome.set_heartbeat_timer(0)
            return ret == DomeCommandStatus.Succeeded

    def close(self):
        print('dome: sending heartbeat ping before closing')
        with self._daemon.connect() as dome:
            dome.set_heartbeat_timer(self._heartbeat_timeout)

        print('dome: closing')
        with self._daemon.connect(timeout=self._movement_timeout) as dome:
            ret = dome.close_shutter()
        return ret == DomeCommandStatus.Succeeded

    def open(self):
        print('dome: sending heartbeat ping before opening')
        with self._daemon.connect() as dome:
            dome.set_heartbeat_timer(self._heartbeat_timeout)

        print('dome: opening')
        with self._daemon.connect(timeout=self._movement_timeout) as dome:
            ret = dome.open_shutter()
        return ret == DomeCommandStatus.Succeeded

