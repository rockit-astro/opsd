#!/usr/bin/env python3
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

"""Telescope action to wait until a specified UTC time"""

# pylint: disable=broad-except
# pylint: disable=invalid-name

import datetime
import threading
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)

class WaitUntil(TelescopeAction):
    """Telescope action to power on and prepare the telescope for observing"""
    def __init__(self, config):
        super().__init__('Waiting', config)
        self._target_date = datetime.datetime.strptime(config['date'], '%Y-%m-%dT%H:%M:%SZ')
        self._wait_condition = threading.Condition()

    @classmethod
    def validation_schema(cls):
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['date'],
            'properties': {
                'type': {'type': 'string'},
                'date': {
                    'type': 'string',
                    'format': 'date-time',
                },
            }
        }

    def run_thread(self):
        """Thread that runs the hardware actions"""
        self.set_task('Waiting until {}'.format(self._target_date.strftime('%H:%M:%S')))
        while True:
            remaining = (self._target_date - datetime.datetime.utcnow()).total_seconds()
            if remaining <= 0 or self.aborted:
                break

            with self._wait_condition:
                self._wait_condition.wait(min(10, remaining))

        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        with self._wait_condition:
            self._wait_condition.notify_all()
