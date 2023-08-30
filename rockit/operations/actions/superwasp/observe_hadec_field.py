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

"""Telescope action to observe a static HA/Dec field within a defined time window"""

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time
import astropy.units as u
from rockit.common import validation
from .mount_helpers import mount_slew_hadec, mount_offset_radec
from .observe_field_base import ObserveFieldBase, ObservationStatus


class ObserveHADecField(ObserveFieldBase):
    """
    Telescope action to observe a static HA/Dec field within a time window

    Example block:
    {
        "type": "ObserveHADecField",
        "start": "2022-09-18T22:20:00",
        "end": "2022-09-18T22:30:00",
        "ha": 0,
        "dec": -4.5,
        "onsky": true, # Optional: defaults to true
        "acquisition": "cam4", # Optional, defaults to no acquisition correction
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
        super().__init__('Observe HA-Dec field', log_name, config)

    def slew_to_field(self):
        """
        Implemented by subclasses to move the mount to the target
        :return: True on success, false on failure
        """
        return mount_slew_hadec(self.log_name, self.config['ha'], self.config['dec'])

    def update_field_pointing(self):
        """
        Implemented by subclasses to update the field pointing based on self._wcs.
        :return: ObservationStatus.OnTarget if acquired,
                 ObservationStatus.PositionLost if another acquisition image is required,
                 ObservationStatus.Error on failure
        """

        lst = Time(self._wcs_field_center.obstime, location=self._wcs_field_center.location).sidereal_time('apparent')
        current = SkyCoord(
            ra=self._wcs_field_center.ra,
            dec=self._wcs_field_center.dec,
            frame='icrs')
        target = SkyCoord(
            ra=lst - self.config['ha'] * u.deg,
            dec=self.config['dec'] * u.deg,
            frame='icrs')

        offset_ra, offset_dec = current.spherical_offsets_to(target)
        print(f'ObserveField: offset is {offset_ra.to_value(u.arcsecond):.1f}, ' +
              f'{offset_dec.to_value(u.arcsecond):.1f}')

        # Close enough!
        if np.abs(offset_ra) < 5 * u.arcmin and np.abs(offset_dec) < 5 * u.arcmin:
            return ObservationStatus.OnTarget

        # Offset telescope
        if not mount_offset_radec(self.log_name, offset_ra.to_value(u.deg), offset_dec.to_value(u.deg)):
            return ObservationStatus.Error

        return ObservationStatus.PositionLost

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = super().config_schema()
        schema['required'].extend(['ha', 'dec'])
        schema['properties'].update({
            'ha': {
                'type': 'number',
                'minimum': -180,
                'maximum': 180
            },
            'dec': {
                'type': 'number',
                'minimum': -30,
                'maximum': 85
            }
        })

        return validation.validation_errors(config_json, schema)
