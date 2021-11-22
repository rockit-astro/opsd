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

"""Telescope action to observe sidereally tracked fields to follow an object defined by a Two Line Element orbit"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import threading

from astropy.coordinates import SkyCoord
from astropy.time import Time, TimeDelta
import astropy.units as u
import astropy.wcs as wcs
import numpy as np
from skyfield.sgp4lib import EarthSatellite
from skyfield.api import Loader, Topos

from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import validation
from warwick.observatory.pipeline import configure_standard_validation_schema as pipeline_schema
from warwick.observatory.camera.fli import configure_validation_schema as fli_camera_schema
from warwick.observatory.camera.qhy import configure_validation_schema as qhy_camera_schema
from .mount_helpers import mount_slew_radec, mount_offset_radec, mount_stop
from .camera_helpers import cam_take_images, cam_stop
from .pipeline_helpers import configure_pipeline

SLEW_TIMEOUT = 120

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = TimeDelta(25, format='sec')

# Amount of time to wait before retrying if an image acquisition generates an error
CAM_ERROR_RETRY_DELAY = TimeDelta(10, format='sec')

# Expected time to converge on target field
SETUP_DELAY = TimeDelta(15, format='sec')

# Time step to use when searching for the target leaving the field of view
FIELD_END_SEARCH_STEP = TimeDelta(5, format='sec')

# Exposure time to use when taking a WCS field image
WCS_EXPOSURE_TIME = TimeDelta(5, format='sec')

# Note: pipeline and camera schemas are inserted in the validate_config method
CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['tle', 'start', 'end'],
    'properties': {
        'type': {'type': 'string'},
        'tle': {
            'type': 'array',
            'maxItems': 3,
            'minItems': 3,
            'items': [
                {
                    'type': 'string',
                },
                {
                    'type': 'string',
                },
                {
                    'type': 'string',
                }
            ]
        },
        'start': {
            'type': 'string',
            'format': 'date-time',
        },
        'end': {
            'type': 'string',
            'format': 'date-time',
        }
    }
}

class WCSStatus:
    Inactive, WaitingForWCS, WCSFailed, WCSComplete = range(4)


class ObserveTLESidereal(TelescopeAction):
    """Telescope action to observe a GEO object by allowing it to trail in front of tracked stars"""
    def __init__(self, log_name, config):
        super().__init__('Observe TLE', log_name, config)
        self._wait_condition = threading.Condition()
        self._camera = 'fli1'

        # TODO: Validate that end > start
        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])

        # Calculate effective field size for calculating pointing offsets and times
        # Ignore a ~300px border around the edges of the field to account for TLE uncertainty
        window = config.get('fli1', {}).get('window', [1, 8176, 1, 6132])
        self._field_width = max(window[1] - window[0] - 600, 100) * 1.57 * u.arcsecond
        self._field_height = max(window[3] - window[2] - 600, 100) * 1.57 * u.arcsecond

        self._target = EarthSatellite(config['tle'][1], config['tle'][2], name=config['tle'][0])
        self._observer = Topos('28.7603135N', '17.8796168 W', elevation_m=2387)
        self._timescale = Loader('/var/tmp').timescale()

        self._wcs_status = WCSStatus.Inactive
        self._wcs = None

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {}
        schema.update(CONFIG_SCHEMA)
        schema['properties']['pipeline'] = pipeline_schema()
        schema['properties']['fli1'] = fli_camera_schema('fli1')
        schema['properties']['cam2'] = qhy_camera_schema('cam2')
        return validation.validation_errors(config_json, schema)

    def __set_failed_status(self):
        """Sets self.status to Complete if aborted otherwise Error"""
        if self.aborted:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def __target_coord(self, target_time):
        """
        Calculate the target RA and Dec at a given time
        :param time: Astropy time to evaluate
        :returns: SkyCoord with the target RA and Dec
        """
        t = self._timescale.from_astropy(target_time)
        ra, dec, _ = (self._target - self._observer).at(t).radec()
        return SkyCoord(ra.to(u.deg), dec.to(u.deg))

    def __field_coord(self, start_time):
        """
        Calculate the RA, Dec that places the target in the corner of the CCD
        at a given time. Returns the Astropy Time that the target leaves the opposite
        corner of the CCD

        :param start_time: Astropy Time to start tracking the object
        :returns:
            SkyCoord defining field center
            Time defining field end
        """
        start_coord = self.__target_coord(start_time)
        end_time = start_time
        end_coord = start_coord

        # Step forward until the target moves outside the requested footprint
        while True:
            test_time = end_time + FIELD_END_SEARCH_STEP
            if end_time > self._end_date:
                break

            test_coord = self.__target_coord(test_time)
            delta_ra, delta_dec = start_coord.spherical_offsets_to(test_coord)
            if np.abs(delta_ra) > self._field_width / np.cos(test_coord.dec) or np.abs(delta_dec) > self._field_height:
                break

            end_time = test_time
            end_coord = test_coord

        # Point in the middle of the start and end
        points = SkyCoord([start_coord, end_coord], unit=u.degree)
        midpoint = SkyCoord(points.data.mean(), frame=points)
        return midpoint, end_time

    def __wait_until_or_aborted(self, target_time):
        """
        Wait until a specified time or the action has been aborted
        :param target: Astropy time to wait for
        :return: True if the time has been reached, false if aborted
        """
        while True:
            remaining = target_time - Time.now()
            if remaining < 0 or self.aborted or not self.dome_is_open:
                break

            with self._wait_condition:
                self._wait_condition.wait(min(10, remaining.to(u.second).value))

        return not self.aborted and self.dome_is_open

    def run_thread(self):
        """Thread that runs the hardware actions"""

        # Configure pipeline immediately so the dashboard can show target name etc
        if not configure_pipeline(self.log_name, self.config.get('pipeline', {}), quiet=True):
            self.__set_failed_status()
            return

        self.set_task('Waiting for observation start')
        self.__wait_until_or_aborted(self._start_date)

        # Remember coordinate offset between pointings
        last_offset_ra = 0
        last_offset_dec = 0
        first_field = True

        while not self.aborted and self.dome_is_open:
            acquire_start = Time.now()
            if acquire_start > self._end_date:
                break

            self.set_task('Acquiring field')
            field_start = acquire_start + SETUP_DELAY
            target_coord, field_end = self.__field_coord(field_start)

            if not mount_slew_radec(self.log_name,
                                    (target_coord.ra + last_offset_ra).to_value(u.deg),
                                    (target_coord.dec + last_offset_dec).to_value(u.deg),
                                    True, SLEW_TIMEOUT):
                print('failed to slew to target')
                self.__set_failed_status()
                return

            # Take a frame to solve field center
            pipeline_config = {}
            pipeline_config.update(self.config.get('pipeline', {}))
            pipeline_config.update({
                'wcs': True,
                'type': 'JUNK',
                'object': 'WCS',
            })

            if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
                self.__set_failed_status()
                return

            cam_config = {}
            cam_config.update(self.config.get(self._camera, {}))
            cam_config.update({
                'exposure': WCS_EXPOSURE_TIME.to(u.second).value,
                'shutter': True,
                'window': [3065, 5112, 2043, 4090]
            })

            # Converge on requested position
            attempt = 1
            while not self.aborted and self.dome_is_open:
                if attempt > 1:
                    self.set_task('Measuring position (attempt {})'.format(attempt))
                else:
                    self.set_task('Measuring position')

                if not cam_take_images(self.log_name, self._camera, 1, cam_config, quiet=True):
                    # Try stopping the camera, waiting a bit, then try again
                    cam_stop(self.log_name, self._camera)
                    self.__wait_until_or_aborted(Time.now() + CAM_ERROR_RETRY_DELAY)
                    attempt += 1
                    if attempt == 6:
                        self.__set_failed_status()
                        return

                # Wait for new frame
                expected_complete = Time.now() + WCS_EXPOSURE_TIME + MAX_PROCESSING_TIME

                # TODO: Locking?
                self._wcs = None
                self._wcs_status = WCSStatus.WaitingForWCS

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
                        print('WCS failed for attempt', attempt)
                    else:
                        print('WCS timed out for attempt', attempt)

                    attempt += 1
                    if attempt == 6:
                        self.__set_failed_status()
                        return

                    continue

                # Calculate frame center and offset from expected pointing
                actual_ra, actual_dec = self._wcs.all_pix2world(1024, 1024, 0, ra_dec_order=True)
                actual_coord = SkyCoord(actual_ra, actual_dec, unit=u.degree)
                offset_ra, offset_dec = actual_coord.spherical_offsets_to(target_coord)

                # Store accumulated offset for the next frame
                last_offset_ra += offset_ra
                last_offset_dec += offset_dec

                # Close enough!
                if offset_ra < 1 * u.arcminute and offset_dec < 1 * u.arcminute:
                    print('offset is {:.1f}, {:.1f} arcsec'.format(
                        offset_ra.to_value(u.arcsecond),
                        offset_dec.to_value(u.arcsecond)))
                    break

                # Offset telescope
                self.set_task('Refining pointing')
                if not mount_offset_radec(self.log_name,
                                          offset_ra.to_value(u.deg),
                                          offset_dec.to_value(u.deg),
                                          SLEW_TIMEOUT):
                    print('failed to offset')
                    self.__set_failed_status()
                    return

            if self.aborted or not self.dome_is_open:
                break

            acquire_delay = (Time.now() - acquire_start).to(u.second).value
            print('Acquired field in {:.1f} seconds'.format(acquire_delay))
            print('Leaves field at {}'.format(field_end))

            # Start science observations
            if not configure_pipeline(self.log_name, self.config.get('pipeline', {}), quiet=not first_field):
                self.__set_failed_status()
                return

            self.set_task('Ends {} / {}'.format(field_end.strftime('%H:%M:%S'), self._end_date.strftime('%H:%M:%S')))
            if not cam_take_images(self.log_name, self._camera, 0, self.config.get(self._camera, {})):
                print('Failed to take_images - will retry for next field')

            first_field = False
            # Wait until the target reaches the edge of the field of view then repeat
            # Don't bother checking for the camera timeout - this is rare
            # and we will catch it on the next field observation if it does happen
            if not self.__wait_until_or_aborted(field_end):
                cam_stop(self.log_name, self._camera)
                print('Failed to wait until end of exposure sequence')
                self.__set_failed_status()
                return

            exposure = self.config.get('fli1', {}).get('exposure', -1)
            cam_stop(self.log_name, self._camera, timeout=exposure + 1)

        exposure = self.config.get('fli1', {}).get('exposure', -1)
        cam_stop(self.log_name, self._camera, timeout=exposure + 1)
        mount_stop(self.log_name)

        self.status = TelescopeActionStatus.Complete

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        with self._wait_condition:
            if self._wcs_status == WCSStatus.WaitingForWCS:
                if 'CRVAL1' in headers:
                    self._wcs = wcs.WCS(headers)
                    self._wcs_status = WCSStatus.WCSComplete
                else:
                    self._wcs_status = WCSStatus.WCSFailed

                self._wait_condition.notify_all()

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        mount_stop(self.log_name)
        cam_stop(self.log_name, self._camera)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()
