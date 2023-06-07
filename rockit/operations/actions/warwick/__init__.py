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

"""Actions that can be scheduled for automated observation"""

from rockit.operations import TelescopeAction, TelescopeActionStatus


# Dummy action that is required by opsd
class ParkTelescope(TelescopeAction):
    def __init__(self, log_name):
        super().__init__('Dummy', log_name, {})

    def run_thread(self):
        """Thread that runs the hardware actions"""
        self.status = TelescopeActionStatus.Complete

    @classmethod
    def validate_config(cls, config_json):
        return []
