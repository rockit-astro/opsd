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

"""Interface to simulate a virtual dome"""
import time
from warwick.observatory.operations.constants import DomeStatus
from warwick.observatory.common import validation


CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': ['module'],
    'required': [
        'open_delay', 'close_delay'
    ],
    'properties': {
        'open_delay': {
            'type': 'number',
            'minimum': 0
        },
        'close_delay': {
            'type': 'number',
            'minimum': 0
        }
    }
}


def validate_config(config_json):
    return validation.validation_errors(config_json, CONFIG_SCHEMA)


class DomeInterface:
    """Interface to simulate a virtual dome"""

    def __init__(self, config):
        self._open_delay = config['open_delay']
        self._close_delay = config['close_delay']
        self._status = DomeStatus.Closed

    def query_status(self):
        return self._status

    def ping_heartbeat(self):
        return True

    def disable_heartbeat(self):
        return True

    def close(self):
        time.sleep(self._close_delay)
        self._status = DomeStatus.Closed
        return True

    def open(self):
        time.sleep(self._open_delay)
        self._status = DomeStatus.Open
        return True

    @classmethod
    def validate_schema(cls, validator, schema, block):
        pass
