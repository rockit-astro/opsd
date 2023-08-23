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

"""Telescope action to park the mount"""

import jsonschema
from rockit.lmount import MountState
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from .mount_helpers import mount_status, mount_stop, mount_park


class ParkTelescope(TelescopeAction):
    """
    Internal action to park the telescope once the actions queue is empty.
    Should not be scheduled manually.
    """
    def __init__(self, log_name):
        super().__init__('Park Telescope', log_name, {})

    def run_thread(self):
        """Thread that runs the hardware actions"""

        status = mount_status(self.log_name)
        if status and 'state' in status and status['state'] not in [MountState.Disabled, MountState.Parked]:
            self.set_task('Parking mount')
            if not mount_stop(self.log_name):
                self.status = TelescopeActionStatus.Error
                return

            if not mount_park(self.log_name):
                self.status = TelescopeActionStatus.Error
                return

        self.status = TelescopeActionStatus.Complete

    @classmethod
    def validate_config(cls, config_json):
        return [jsonschema.exceptions.SchemaError('ParkTelescope cannot be scheduled directly')]
