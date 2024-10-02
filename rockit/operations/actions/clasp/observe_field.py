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

"""Telescope action to observe a sidereal field within a defined time window"""

from rockit.common import validation
from .mount_helpers import mount_slew_radec
from .observe_field_base import ObserveFieldBase


class ObserveField(ObserveFieldBase):
    """
    Telescope action to observe a sidereal field within a time window

    Example block:
    {
        "type": "ObserveField",
        "start": "2022-09-18T22:20:00",
        "end": "2022-09-18T22:30:00",
        "ra": 0,
        "dec": -4.5,
        "onsky": true, # Optional: defaults to true
        "cam1": { # Optional: cameras that aren't listed won't be focused
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, window, gain, offset, stream (advanced options)
        },
        "cam2": { # Optional: cameras that aren't listed won't be focused
            "exposure": 1,
            # Also supports optional temperature (advanced options)
        },
        "pipeline": {
           "prefix": "survey",
           "object": "HA 0",
           "archive": ["CAM1"] # Optional: defaults to the cameras defined in the action
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, **args):
        super().__init__('Observe field', **args)

    def slew_to_field(self):
        """
        Implemented by subclasses to move the mount to the target
        :return: True on success, false on failure
        """
        return mount_slew_radec(self.log_name, self.config['ra'], self.config['dec'], True)

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = super().config_schema()
        schema['required'].extend(['ra', 'dec'])
        schema['properties'].update({
            'ra': {
                'type': 'number',
                'minimum': 0,
                'maximum': 360
            },
            'dec': {
                'type': 'number',
                'minimum': -40,
                'maximum': 85
            }
        })

        return validation.validation_errors(config_json, schema)
