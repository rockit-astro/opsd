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

"""Placeholder action that does nothing"""

from rockit.common import validation
from rockit.operations import TelescopeAction, TelescopeActionStatus

class ParkTelescope(TelescopeAction):
    def __init__(self, **args):
        super().__init__('Park Telescope', **args)

    def run_thread(self):
        """Thread that runs the hardware actions"""
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
