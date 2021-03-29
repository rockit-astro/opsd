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

"""Telescope action to park the telescope and switch off the drive power"""

from warwick.observatory.common import daemons
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)
from warwick.rasa.telescope import CommandStatus as TelCommandStatus
from .telescope_helpers import tel_park_stow


class Shutdown(TelescopeAction):
    """Telescope action to park the telescope and switch off the drive power"""
    def __init__(self, config):
        super().__init__('Shutdown', {})

    @classmethod
    def validation_schema(cls):
        return {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'type': {'type': 'string'}
            }
        }

    def run_thread(self):
        """Thread that runs the hardware actions"""

        self.set_task('Parking Telescope')
        if not tel_park_stow(self.log_name):
            self.status = TelescopeActionStatus.Error
            return

        self.set_task('Shutting down')

        with daemons.rasa_telescope.connect() as teld:
            status = teld.shutdown()
            if status not in [TelCommandStatus.Succeeded,
                              TelCommandStatus.TelescopeNotEnabled]:
                print('Failed to shutdown telescope')
                self.status = TelescopeActionStatus.Error
                return

        # TODO: Warm up camera, disable camera/focuser, turn off power

        self.status = TelescopeActionStatus.Complete
