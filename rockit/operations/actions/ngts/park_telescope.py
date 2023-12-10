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

"""Telescope action to park the telescope"""

from rockit.common import validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from rockit.talon import TelState
from .mount_helpers import mount_status, mount_stop, mount_park


class ParkTelescope(TelescopeAction):
    """
    Internal action to park the telescope once the actions queue is empty.
    Can also be manually scheduled.
    """
    def __init__(self, log_name, _=None):
        super().__init__('Park Telescope', log_name, {})

    def run_thread(self):
        """Thread that runs the hardware actions"""
        status = mount_status(self.log_name)
        if status and 'state' in status and status['state'] != TelState.Absent:
            if not mount_stop(self.log_name):
                self.status = TelescopeActionStatus.Error
                return

            if not mount_park(self.log_name):
                self.status = TelescopeActionStatus.Error
                return

        self.status = TelescopeActionStatus.Complete

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'type': {'type': 'string'}
            }
        }

        return validation.validation_errors(config_json, schema)
