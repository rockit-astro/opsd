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

"""Telescope action to offset the telescope by a given ra, dec"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import math
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)
from .telescope_helpers import tel_offset_radec, tel_stop

OFFSET_TIMEOUT = 120

class OffsetTelescope(TelescopeAction):
    """Telescope action to offset the telescope by a given ra, dec"""
    def __init__(self, config):
        super().__init__('Offset Telescope', config)

    @classmethod
    def validation_schema(cls):
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['ra', 'dec'],
            'properties': {
                'type': {'type': 'string'},
                'ra': {
                    'type': 'number',
                    'minimum': -2 * math.pi,
                    'maximum': 2 * math.pi
                },
                'dec': {
                    'type': 'number',
                    'minimum': -math.pi,
                    'maximum': math.pi
                },
            }
        }

    def run_thread(self):
        """Thread that runs the hardware actions"""
        self.set_task('Slewing')
        if not tel_offset_radec(self.log_name, self.config['ra'], self.config['dec'],
                                OFFSET_TIMEOUT):
            self.status = TelescopeActionStatus.Error
            return

        if not self.aborted:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def abort(self):
        """Aborted by a weather alert or user action"""
        super().abort()
        tel_stop(self.log_name)
