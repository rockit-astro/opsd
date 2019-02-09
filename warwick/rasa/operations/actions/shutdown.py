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

from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)
from .telescope_helpers import tel_slew_altaz

# Position to park the telescope after homing
STOW_ALTAZ = (0.616, 0.405)
STOW_TIMEOUT = 60

class Shutdown(TelescopeAction):
    """Telescope action to park the telescope and switch off the drive power"""
    def __init__(self):
        super().__init__('Shutdown', {})

    def run_thread(self):
        """Thread that runs the hardware actions"""
        self.set_task('Parking Telescope')
        if not tel_slew_altaz(self.log_name, STOW_ALTAZ[0], STOW_ALTAZ[1],
                              False, STOW_TIMEOUT):
            self.status = TelescopeActionStatus.Error
            return
        self.status = TelescopeActionStatus.Complete
