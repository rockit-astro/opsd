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

"""Telescope action to slew the telescope to a given alt az and add a pointing model point"""

import threading

from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time
import astropy.units as u
import astropy.wcs as wcs

from warwick.observatory.common import validation
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cam_take_images
from .mount_helpers import mount_slew_radec, mount_stop, mount_status, mount_add_pointing_model_point
from .pipeline_helpers import configure_pipeline

SLEW_TIMEOUT = 120

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = 45 * u.s

CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['alt', 'az', 'camera', 'exposure', 'refx', 'refy'],
    'properties': {
        'type': {'type': 'string'},
        'az': {
            'type': 'number',
            'minimum': 0,
            'maximum': 360
        },
        'alt': {
            'type': 'number',
            'minimum': 0,
            'maximum': 90
        },
        'camera': {
            'type': 'string',
            'enum': ['cam1', 'cam2']
        },
        'exposure': {
            'type': 'number',
            'minimum': 0
        },
        'refx': {
            'type': 'number',
            'minimum': 0
        },
        'refy': {
            'type': 'number',
            'minimum': 0
        }
    }
}


class WCSStatus:
    Inactive, WaitingForWCS, WCSFailed, WCSComplete = range(4)


class PointingModelPointing(TelescopeAction):
    """Telescope action to slew the telescope to a given alt az and add a pointing model point"""
    def __init__(self, log_name, config):
        super().__init__('Pointing Model', log_name, config)
        self._wait_condition = threading.Condition()
        self._wcs_status = WCSStatus.Inactive
        self._wcs = None

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        return validation.validation_errors(config_json, CONFIG_SCHEMA)

    def run_thread(self):
        """Thread that runs the hardware actions"""

        status = mount_status(self.log_name)
        location = EarthLocation(
            lat=status['site_latitude'],
            lon=status['site_longitude'],
            height=status['site_elevation'])

        # Convert the requested altaz to radec that we track for the measurement
        coords = SkyCoord(alt=self.config['alt'], az=self.config['az'], unit=u.deg, frame='altaz',
                          location=location, obstime=Time.now())

        self.set_task('Slewing')
        if not mount_slew_radec(self.log_name, coords.icrs.ra.to_value(u.deg), coords.icrs.dec.to_value(u.deg),
                                True, SLEW_TIMEOUT):
            self.status = TelescopeActionStatus.Complete
            return

        # Take a frame to solve field center
        pipeline_config = {
            'wcs': True,
            'type': 'JUNK',
            'object': 'WCS',
        }

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        cam_config = {
            'exposure': self.config['exposure']
        }

        attempt = 1
        while not self.aborted and self.dome_is_open:
            if attempt > 1:
                self.set_task('Measuring position (attempt {})'.format(attempt))
            else:
                self.set_task('Measuring position')

            self._wcs = None
            self._wcs_status = WCSStatus.WaitingForWCS

            print('PointingModelPointing: taking image')
            if not cam_take_images(self.log_name, self.config['camera'], 1, cam_config, quiet=True):
                self.status = TelescopeActionStatus.Error
                return

            # Wait for new frame
            expected_complete = Time.now() + self.config['exposure'] + MAX_PROCESSING_TIME

            while True:
                with self._wait_condition:
                    remaining = expected_complete - Time.now()
                    if remaining < 0 or self._wcs_status != WCSStatus.WaitingForWCS:
                        break

                    self._wait_condition.wait(max(remaining.to(u.second).value, 1))

            failed = self._wcs_status == WCSStatus.WCSFailed
            timeout = self._wcs_status == WCSStatus.WaitingForWCS
            self._wcs_status = WCSStatus.Inactive

            if failed or timeout:
                if failed:
                    print('PointingModelPointing: WCS failed for attempt', attempt)
                else:
                    print('PointingModelPointing: WCS timed out for attempt', attempt)

                attempt += 1
                if attempt == 6:
                    self.status = TelescopeActionStatus.Complete
                    return
                continue

            actual_ra, actual_dec = self._wcs.all_pix2world(self.config['refx'], self.config['refy'],
                                                            0, ra_dec_order=True)

            mount_add_pointing_model_point(self.log_name, actual_ra.item(), actual_dec.item())
            self.status = TelescopeActionStatus.Complete
            break

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        mount_stop(self.log_name)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        if headers.get('CAMID', '').lower() != self.config['camera']:
            return

        with self._wait_condition:
            if self._wcs_status == WCSStatus.WaitingForWCS:
                if 'CRVAL1' in headers:
                    self._wcs = wcs.WCS(headers)
                    self._wcs_status = WCSStatus.WCSComplete
                else:
                    self._wcs_status = WCSStatus.WCSFailed

                self._wait_condition.notify_all()
