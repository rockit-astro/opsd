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

"""Telescope action to wait until the dome is open (or a timeout to expire)"""

import datetime
import threading
from warwick.observatory.common import validation
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus

CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'type': {'type': 'string'},
        'timeout': {
            'type': 'number',
            'minimum': 0
        }
    }
}

class WaitForDome(TelescopeAction):
    """Telescope action to wait until the dome is open (or a timeout to expire)"""
    def __init__(self, log_name, config):
        super().__init__('Waiting', log_name, config)
        self._wait_condition = threading.Condition()

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        return validation.validation_errors(config_json, CONFIG_SCHEMA)

    def run_thread(self):
        """Thread that runs the hardware actions"""
        timeout = None
        if 'timeout' in self.config:
            timeout = datetime.datetime.utcnow() + datetime.timedelta(seconds=self.config['timeout'])

        while True:
            if self.dome_is_open:
                break

            task = 'Waiting for dome'
            if timeout:
                remaining = (datetime.datetime.utcnow() - timeout).total_seconds()
                task += ' ({:.0f}s remaining)'.format(remaining)
            self.set_task(task)

            with self._wait_condition:
                self._wait_condition.wait(10)

            if not self.dome_is_open and timeout and datetime.datetime.utcnow() > timeout:
                self.status = TelescopeActionStatus.Error
                return

        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()
