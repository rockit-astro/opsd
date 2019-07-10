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

"""Telescope action to observe a sidereally tracked field"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import threading

from astropy.time import Time, TimeDelta
import astropy.units as u
import astropy.wcs as wcs

from warwick.observatory.common import daemons, log
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)
from warwick.rasa.camera import (
    CameraStatus,
    configure_validation_schema as camera_schema)
from warwick.rasa.pipeline import (
    configure_standard_validation_schema as pipeline_schema)

from .camera_helpers import take_images, stop_camera, get_camera_status
from .pipeline_helpers import configure_pipeline
from .telescope_helpers import tel_slew_radec, tel_offset_radec, tel_stop

SLEW_TIMEOUT = 120

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = TimeDelta(25, format='sec')

# Amount of time to wait before retrying if an image acquisition generates an error
CAM_ERROR_RETRY_DELAY = TimeDelta(10, format='sec')

# Expected time to converge on target field
SETUP_DELAY = TimeDelta(15, format='sec')

# Exposure time to use when taking a WCS field image
WCS_EXPOSURE_TIME = TimeDelta(5, format='sec')

# Amount of time to wait between camera status checks while observing
CAM_CHECK_STATUS_DELAY = TimeDelta(60, format='sec')

VALID_CAMERA_STATES = [CameraStatus.Acquiring, CameraStatus.Reading, CameraStatus.Waiting]


class WCSStatus:
    Inactive, WaitingForWCS, WCSFailed, WCSComplete = range(4)


class ObserveField(TelescopeAction):
    """Telescope action to observe a sidereally tracked field"""
    def __init__(self, config):
        super().__init__('Observe Field', config)
        self._wait_condition = threading.Condition()
        self._camera = daemons.rasa_camera

        # TODO: Validate that end > start
        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])

        self._wcs_status = WCSStatus.Inactive
        self._wcs = None

    @classmethod
    def validation_schema(cls):
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['start', 'end', 'ra', 'dec', 'rasa', 'pipeline'],
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
                'ra': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 360
                },
                'dec': {
                    'type': 'number',
                    'minimum': -90,
                    'maximum': 90
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

        acquire_start = Time.now()
        if acquire_start > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        self.set_task('Acquiring field')
        target_ra = self.config['ra'] * u.degree
        target_dec = self.config['dec'] * u.degree

        if not tel_slew_radec(self.log_name,
                              target_ra.to_value(u.rad),
                              target_dec.to_value(u.rad),
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
            offset_ra = target_ra - actual_ra * u.degree
            offset_dec = target_dec - actual_dec * u.degree

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
            self.__set_failed_status()

        acquire_delay = (Time.now() - acquire_start).to(u.second).value
        print('Acquired field in {:.1f} seconds'.format(acquire_delay))

        # Start science observations
        if not configure_pipeline(self.log_name, self.config.get('pipeline', {})):
            self.__set_failed_status()
            return

        self.set_task('Ends {}'.format(self._end_date.strftime('%H:%M:%S')))
        attempt = 1
        while True:
            if Time.now() > self._end_date:
                break

            if not take_images(self.log_name, self._camera, 0, self.config.get('rasa', {})):
                # Try stopping the camera, waiting a bit, then try again
                stop_camera(self.log_name, self._camera)
                self.__wait_until_or_aborted(Time.now() + CAM_ERROR_RETRY_DELAY)
                attempt += 1
                if attempt < 6:
                    continue

                self.__set_failed_status()
                return

            while True:
                # Check camera for error status every minute
                if not self.__wait_until_or_aborted(Time.now() + CAM_CHECK_STATUS_DELAY):
                    stop_camera(self.log_name, self._camera)
                    print('Camera wait aborted')
                    self.__set_failed_status()
                    return

                if Time.now() > self._end_date:
                    break

                status = get_camera_status(self.log_name, self._camera)
                if not status:
                    print('Failed to query camera status')
                    log.error(self.log_name, 'Failed to query camera status')
                    continue

                if status['state'] not in VALID_CAMERA_STATES:
                    message = 'Camera is in unexpected state', CameraStatus.label(status['state'])
                    print(message)
                    log.error(self.log_name, message)

                    if status['state'] == CameraStatus.Idle:
                        print('Restarting exposures')
                        log.info(self.log_name, 'Restarting exposures')
                        break

        exposure = self.config.get('rasa', {}).get('exposure', -1)
        stop_camera(self.log_name, self._camera, timeout=exposure + 1)
        tel_stop(self.log_name)

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
