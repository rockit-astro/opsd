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

"""Telescope action to observe a static Alt/Az field within a defined time window"""

from warwick.observatory.common import validation
from .mount_helpers import mount_slew_altaz
from .observe_field_base import ObserveFieldBase


class ObserveAltAzField(ObserveFieldBase):
    """
    Telescope action to observe a static Alt/Az field within a time window

    Example block:
    {
        "type": "ObserveAltAzField",
        "start": "2022-09-18T22:20:00",
        "end": "2022-09-18T22:30:00",
        "alt": 40,
        "az": 180,
        "onsky": true, # Optional: defaults to true
        "cam<1..4>": { # Optional: cameras that aren't listed won't be used
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, gain, offset, stream (advanced options)
        },
        "pipeline": {
           "prefix": "survey",
           "object": "HA 0",
           "archive": ["CAM1"] # Optional: defaults to the cameras defined in the action
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, log_name, config):
        super().__init__('Observe Alt-Az field', log_name, config)

    def slew_to_field(self):
        """
        Implemented by subclasses to move the mount to the target
        :return: True on success, false on failure
        """
        return mount_slew_altaz(self.log_name, self.config['alt'], self.config['az'], False)

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = super().config_schema()
        schema['required'].extend(['alt', 'az'])
        schema['properties'].update({
            'alt': {
                'type': 'number',
                'minimum': 0,
                'maximum': 90
            },
            'az': {
                'type': 'number',
                'minimum': 0,
                'maximum': 360
            }
        })

        return validation.validation_errors(config_json, schema)
