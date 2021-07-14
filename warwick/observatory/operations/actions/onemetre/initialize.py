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

"""Telescope action to power on and prepare the telescope for observing"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods

from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)

class Initialize(TelescopeAction):
    """Telescope action to power on and prepare the telescope for observing"""
    def __init__(self, log_name):
        super().__init__('Initializing', log_name, {})

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # TODO: Implement init logic
        self.status = TelescopeActionStatus.Complete
