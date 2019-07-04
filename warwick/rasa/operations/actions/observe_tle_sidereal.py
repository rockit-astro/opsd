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

from astropy.time import Time, TimeDelta
import astropy.units as u
import astropy.wcs as wcs
import numpy as np
from skyfield.sgp4lib import EarthSatellite
from skyfield.api import Loader, Topos

from warwick.observatory.common import daemons
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)
from warwick.rasa.camera import (
    configure_validation_schema as camera_schema)
from warwick.rasa.pipeline import (
    configure_standard_validation_schema as pipeline_schema)

from warwick.rasa.operations.actions.camera_helpers import take_images, stop_camera
from warwick.rasa.operations.actions.pipeline_helpers import configure_pipeline
from warwick.rasa.operations.actions.telescope_helpers import tel_slew_radec, tel_offset_radec, tel_stop

SLEW_TIMEOUT = 120

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = TimeDelta(25, format='sec')

# Time to allow for the camera to complete a stop request
ABORT_SEQUENCE_DELAY = TimeDelta(2.5, format='sec')

# Expected time to converge on target field
SETUP_DELAY = TimeDelta(15, format='sec')

# Time step to use when searching for the target leaving the field of view
FIELD_END_SEARCH_STEP = TimeDelta(5, format='sec')

# Exposure time to use when taking a WCS field image
WCS_EXPOSURE_TIME = TimeDelta(5, format='sec')

class WCSStatus:
    Inactive, WaitingForWCS, WCSFailed, WCSComplete = range(4)

class ObserveTLESidereal(TelescopeAction):
    """Telescope action to observe a GEO object by allowing it to trail in front of tracked stars"""
    def __init__(self, config):
        self._target_name = config['tle'][0]

        # SpaceTrack TLEs include a leading '0 ' in the target name
        if self._target_name.startswith('0 '):
            self._target_name = self._target_name[2:]

        super().__init__(self._target_name, config)
        self._wait_condition = threading.Condition()
        self._camera = daemons.rasa_camera

        # TODO: Validate that end > start
        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])

        # Calculate effective field size for calculating pointing offsets and times
        # Ignore a ~300px border around the edges of the field to account for TLE uncertainty
        window = config.get('rasa', {}).get('window', [1, 8176, 1, 6132])
        self._field_width = max(window[1] - window[0] - 600, 100) * 1.57 * u.arcsecond
        self._field_height = max(window[3] - window[2] - 600, 100) * 1.57 * u.arcsecond

        self._target = EarthSatellite(config['tle'][1], config['tle'][2], name=config['tle'][0])
        self._observer = Topos('28.7603135N', '17.8796168 W', elevation_m=2387)
        self._timescale = Loader('/var/tmp').timescale()

        self._wcs_status = WCSStatus.Inactive
        self._wcs = None

    @classmethod
    def validation_schema(cls):
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['tle', 'start', 'end'],
            'properties': {
                'type': {'type': 'string'},
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'end': {
                    'type': 'string',
                    'format': 'date-time',
                },
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
                'rasa': camera_schema('rasa'),
                'pipeline': pipeline_schema()
            }
        }

    def __set_failed_status(self):
        """Sets self.status to Complete if aborted otherwise Error"""
        if self.aborted:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def __target_radec(self, target_time):
        """
        Calculate the target RA and Dec at a given time
        :param time: Astropy time to evaluate
        :returns: RA, Dec Astropy quantities
        """
        t = self._timescale.from_astropy(target_time)
        ra, dec, _ = (self._target - self._observer).at(t).radec()
        return ra.to(u.degree), dec.to(u.degree)


    def __field_radec(self, start_time):
        """
        Calculate the RA, Dec that places the target in the corner of the CCD
        at a given time. Returns the Astropy Time that the target leaves the opposite
        corner of the CCD

        :param start_time: Astropy Time to start tracking the object
        :returns:
            RA
            Dec
            end_time
        """
        start_ra, start_dec = self.__target_radec(start_time)
        end_time = start_time
        end_ra = start_ra
        end_dec = start_dec

        # Step forward until the target moves outside the requested footprint
        while True:
            test_time = end_time + FIELD_END_SEARCH_STEP
            if end_time > self._end_date:
                break

            test_ra, test_dec = self.__target_radec(test_time)
            delta_ra = test_ra - start_ra
            delta_dec = test_dec - start_dec
            if delta_ra > self._field_width / np.cos(test_dec) or delta_dec > self._field_height:
                break

            end_time = test_time
            end_ra = test_ra
            end_dec = test_dec

        # Point in the middle of the start and end
        field_ra = (start_ra + end_ra) / 2
        field_dec = (start_dec + end_dec) / 2
        return field_ra, field_dec, end_time

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
            target_ra, target_dec, field_end = self.__field_radec(field_start)

            if not tel_slew_radec(self.log_name,
                                  (target_ra + last_offset_ra).to_value(u.rad),
                                  (target_dec + last_offset_dec).to_value(u.rad),
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
                'archive': {
                    'RASA': False
                }
            })

            if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
                self.__set_failed_status()
                return

            cam_config = {}
            cam_config.update(self.config.get('rasa', {}))
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

                if not take_images(self.log_name, self._camera, 1, cam_config, quiet=True):
                    # Try stopping the camera, waiting a bit, then try again
                    stop_camera(self.log_name, self._camera)
                    self.__wait_until_or_aborted(Time.now() + ABORT_SEQUENCE_DELAY)
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
                offset_ra = target_ra - actual_ra * u.degree
                offset_dec = target_dec - actual_dec * u.degree

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
                if not tel_offset_radec(self.log_name,
                                        offset_ra.to_value(u.rad),
                                        offset_dec.to_value(u.rad),
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
            pipeline_config = {}
            pipeline_config.update(self.config.get('pipeline', {}))
            pipeline_config.update({
                'type': 'SCIENCE',
                'object': self._target_name,
            })

            if not configure_pipeline(self.log_name, pipeline_config, quiet=not first_field):
                self.__set_failed_status()
                return

            self.set_task('Observing')
            if not take_images(self.log_name, self._camera, 0, self.config.get('rasa', {})):
                print('Failed to take_images - will retry for next field')

            first_field = False
            # Wait until the target reaches the edge of the field of view then repeat
            # Don't bother checking for the camera timeout - this is rare
            # and we will catch it on the next field observation if it does happen
            if not self.__wait_until_or_aborted(field_end):
                stop_camera(self.log_name, self._camera)
                print('Failed to wait until end of exposure sequence')
                self.__set_failed_status()
                return

            stop_camera(self.log_name, self._camera)
            self.__wait_until_or_aborted(Time.now() + ABORT_SEQUENCE_DELAY)

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

        tel_stop(self.log_name)
        stop_camera(self.log_name, self._camera)

        with self._wait_condition:
            self._wait_condition.notify_all()
