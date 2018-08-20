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

"""Telescope action to wait for a specified amount of time"""

# pylint: disable=broad-except
# pylint: disable=invalid-name

import threading
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)

class Wait(TelescopeAction):
    """Telescope action to power on and prepare the telescope for observing"""
    def __init__(self, config):
        super().__init__('Waiting', config)
        self._wait_condition = threading.Condition()

    @classmethod
    def validation_schema(cls):
        return {
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

    def run_thread(self):
        """Thread that runs the hardware actions"""
        with self._wait_condition:
            self._wait_condition.wait(self.config['delay'])

        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Aborted by a weather alert or user action"""
        super().abort()
        with self._wait_condition:
            self._wait_condition.notify_all()
