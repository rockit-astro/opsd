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

"""Telescope action to park the telescope"""

from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from .mount_helpers import mount_stop, mount_park


class ParkTelescope(TelescopeAction):
    """Telescope action to park the telescope"""
    def __init__(self, log_name):
        super().__init__('Park Telescope', log_name, {})

    def run_thread(self):
        """Thread that runs the hardware actions"""
        self.set_task('Parking')
        if not mount_stop(self.log_name):
            self.status = TelescopeActionStatus.Error
            return

        if not mount_park(self.log_name):
            self.status = TelescopeActionStatus.Error
            return

        self.status = TelescopeActionStatus.Complete
