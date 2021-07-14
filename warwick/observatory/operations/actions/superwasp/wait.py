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

"""Telescope action to wait for a specified amount of time"""

import datetime
import threading
from warwick.observatory.common import validation
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus

CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['delay'],
    'properties': {
        'type': {'type': 'string'},
        'delay': {
            'type': 'number',
            'minimum': 0
        },
    }
}


class Wait(TelescopeAction):
    """Telescope action to power on and prepare the telescope for observing"""
    def __init__(self, config):
        super().__init__('Waiting', config)
        self._wait_condition = threading.Condition()

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        return validation.validation_errors(config_json, CONFIG_SCHEMA)

    def run_thread(self):
        """Thread that runs the hardware actions"""
        timeout = datetime.datetime.utcnow() + datetime.timedelta(seconds=self.config['delay'])
        while True:
            remaining = (timeout - datetime.datetime.utcnow()).total_seconds()
            if remaining < 0 or self.aborted:
                break

            self.set_task('Waiting ({:.0f}s remaining)'.format(remaining))
            with self._wait_condition:
                self._wait_condition.wait(10)

        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        with self._wait_condition:
            self._wait_condition.notify_all()
