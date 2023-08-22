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

"""Interface to allow the dome controller to operate SuperWASP's roof"""

from warwick.observatory.operations.constants import DomeStatus
from rockit.common import daemons, validation
from rockit.roof import RoofStatus, HeartbeatStatus, CommandStatus as RoofCommandStatus

CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': ['module'],
    'required': [
        'daemon', 'open_timeout', 'close_timeout',
        'heartbeat_timeout', 'heartbeat_open_timeout', 'heartbeat_close_timeout'
    ],
    'properties': {
        'daemon': {
            'type': 'string',
            'daemon_name': True
        },
        'open_timeout': {
            'type': 'number',
            'minimum': 0
        },
        'close_timeout': {
            'type': 'number',
            'minimum': 0
        },
        'heartbeat_timeout': {
            'type': 'number',
            'minimum': 0
        },
        'heartbeat_open_timeout': {
            'type': 'number',
            'minimum': 0
        },
        'heartbeat_close_timeout': {
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

        # Communications timeout when opening or closing the roof
        self._open_timeout = dome_config_json['open_timeout']
        self._close_timeout = dome_config_json['close_timeout']

        # Timeout period (seconds) for the roof controller
        # The dome heartbeat is pinged once per LOOP_DELAY when the roof is under
        # automatic control and is fully open or fully closed.
        self._heartbeat_timeout = dome_config_json['heartbeat_timeout']
        self._heartbeat_open_timeout = dome_config_json['heartbeat_open_timeout']
        self._heartbeat_close_timeout = dome_config_json['heartbeat_close_timeout']

    def query_status(self):
        with self._daemon.connect() as roof:
            status = roof.status()

        if status['heartbeat_status'] == HeartbeatStatus.TimedOut:
            return DomeStatus.Timeout

        if status['status'] == RoofStatus.Closed:
            return DomeStatus.Closed

        if status['status'] in [RoofStatus.Opening, RoofStatus.Closing]:
            return DomeStatus.Moving

        return DomeStatus.Open

    def ping_heartbeat(self):
        print('roof: sending heartbeat ping')
        with self._daemon.connect() as roof:
            ret = roof.set_heartbeat_timer(self._heartbeat_timeout)
            return ret == RoofCommandStatus.Succeeded

    def disable_heartbeat(self):
        print('roof: disabling heartbeat')
        with self._daemon.connect() as roof:
            ret = roof.set_heartbeat_timer(0)
            return ret == RoofCommandStatus.Succeeded

    def close(self):
        print('roof: sending heartbeat ping before closing')
        with self._daemon.connect() as roof:
            roof.set_heartbeat_timer(self._heartbeat_close_timeout)

        print('roof: closing')
        with self._daemon.connect(timeout=self._close_timeout) as roof:
            ret = roof.close()
        return ret == RoofCommandStatus.Succeeded

    def open(self):
        print('roof: sending heartbeat ping before opening')
        with self._daemon.connect() as roof:
            roof.set_heartbeat_timer(self._heartbeat_open_timeout)

        print('roof: opening')
        with self._daemon.connect(timeout=self._open_timeout) as roof:
            ret = roof.open()

        return ret == RoofCommandStatus.Succeeded
