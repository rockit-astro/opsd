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

"""Base logic for observe_*_field telescope actions"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import re
import threading
import time
from astropy import wcs
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time
import astropy.units as u
import jsonschema
from warwick.observatory.camera.qhy import CameraStatus
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import log
from .camera_helpers import cameras, cam_initialize, cam_status, cam_configure, cam_take_images, cam_stop, \
    cam_cycle_power, cam_reinitialize_synchronised, cam_start_synchronised, cam_stop_synchronised
from .mount_helpers import mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_science_schema

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = 60 * u.s

# Amount of time to wait before retrying if an image acquisition generates an error
CAM_ERROR_RETRY_DELAY = 10 * u.s

# Exposure time to use when taking a WCS field image
WCS_EXPOSURE_TIME = 5 * u.s

# Amount of time to wait between camera status checks while observing
CAM_CHECK_STATUS_DELAY = 10 * u.s


class ObserveFieldBase(TelescopeAction):
    """
    Base field observation logic that is inherited by other telescope actions.
    Should not be scheduled directly.
    """
    def __init__(self, action_name, log_name, config):
        super().__init__(action_name, log_name, config)
        self._wait_condition = threading.Condition()

        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])
        self._camera_ids = [c for c in cameras if c in self.config]
        self._acquisition_camera = config.get('acquisition', None)

        self._observation_status = ObservationStatus.PositionLost
        self._last_exposure_started = {camera_id: Time.now() for camera_id in self._camera_ids}

        self._wcs_status = WCSStatus.Inactive
        self._wcs = None
        self._wcs_field_center = None

    def slew_to_field(self):
        """
        Implemented by subclasses to move the mount to the target
        :return: True on success, false on failure
        """
        return False

    def update_field_pointing(self):
        """
        Implemented by subclasses to update the field pointing based on self._wcs.
        :return: ObservationStatus.OnTarget if acquired,
                 ObservationStatus.PositionLost if another acquisition image is required,
                 ObservationStatus.Error on failure
        """
        return ObservationStatus.OnTarget

    def __acquire_field(self):
        self.set_task('Acquiring field')

        # Point to the requested location
        acquire_start = Time.now()
        print('ObserveField: slewing to target field')
        if not self.slew_to_field():
            return ObservationStatus.Error

        if self._acquisition_camera is None or not self.config.get('onsky', True):
            return ObservationStatus.OnTarget

        # Take a frame to solve field center
        pipeline_config = {
            'wcs': True,
            'type': 'JUNK',
            'object': 'WCS',
        }

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            return ObservationStatus.Error

        cam_config = {}
        cam_config.update(self.config.get(self._acquisition_camera, {}))
        cam_config.update({
            'exposure': WCS_EXPOSURE_TIME.to(u.second).value,
            'stream': False
        })

        # Acquisition images are always full-frame
        if 'window' in cam_config:
            cam_config.pop('window')

        # Converge on requested position
        attempt = 1
        while not self.aborted and self.dome_is_open:
            # Wait for telescope position to settle before taking first image
            time.sleep(5)

            if attempt > 1:
                self.set_task(f'Measuring position (attempt {attempt})')
            else:
                self.set_task('Measuring position')

            self._wcs = None
            self._wcs_status = WCSStatus.WaitingForWCS

            print('ObserveField: taking test image')
            while not cam_take_images(self.log_name, self._acquisition_camera, 1, cam_config, quiet=True):
                # Try stopping the camera, waiting a bit, then try again
                cam_stop(self.log_name, self._acquisition_camera)
                self.wait_until_time_or_aborted(Time.now() + CAM_ERROR_RETRY_DELAY, self._wait_condition)
                if self.aborted or not self.dome_is_open:
                    break

                attempt += 1
                if attempt == 6:
                    return ObservationStatus.Error

            if self.aborted or not self.dome_is_open:
                break

            # Wait for new frame
            expected_complete = Time.now() + WCS_EXPOSURE_TIME + MAX_PROCESSING_TIME

            while True:
                with self._wait_condition:
                    remaining = expected_complete - Time.now()
                    if remaining < 0 * u.s or self._wcs_status != WCSStatus.WaitingForWCS:
                        break

                    self._wait_condition.wait(max(remaining.to(u.second).value, 1))

            if self.aborted or not self.dome_is_open:
                break

            failed = self._wcs_status == WCSStatus.WCSFailed
            timeout = self._wcs_status == WCSStatus.WaitingForWCS
            self._wcs_status = WCSStatus.Inactive

            if failed or timeout:
                if failed:
                    print('ObserveField: WCS failed for attempt', attempt)
                else:
                    print('ObserveField: WCS timed out for attempt', attempt)

                attempt += 1
                if attempt == 6:
                    return ObservationStatus.Error

                continue

            result = self.update_field_pointing()
            if result == ObservationStatus.Error:
                return ObservationStatus.Error

            if result == ObservationStatus.OnTarget:
                # Reset the acquisition camera to streaming mode now
                # to avoid the delay later putting it out of sync with the others
                cam_config = self.config.get(self._acquisition_camera, {})
                cam_configure(self.log_name, self._acquisition_camera, cam_config, quiet=True)

                dt = (Time.now() - acquire_start).to(u.s).value
                print(f'ObserveField: Acquired field in {dt:.1f} seconds')
                return ObservationStatus.OnTarget

        if not self.dome_is_open:
            return ObservationStatus.DomeClosed

        if self.aborted:
            return ObservationStatus.Complete

        return ObservationStatus.Error

    def __wait_for_dome(self):
        while True:
            with self._wait_condition:
                if Time.now() > self._end_date or self.aborted:
                    return ObservationStatus.Complete

                if self.dome_is_open:
                    return ObservationStatus.PositionLost

                self._wait_condition.wait(10)

    def __start_exposures(self):
        success = cam_reinitialize_synchronised(self.log_name, self._camera_ids)
        for camera_id in self._camera_ids:
            success = success and cam_configure(self.log_name, camera_id, self.config.get(camera_id, None), quiet=True)

        if not success:
            return False

        return cam_start_synchronised(self.log_name, self._camera_ids)

    def __check_timeouts(self, camera_id):
        timeout = self._last_exposure_started[camera_id] + self.config[camera_id]['exposure'] * u.s + \
                  MAX_PROCESSING_TIME
        if Time.now() < timeout:
            return True

        # Exposure has timed out: lets find out why
        status = cam_status(self.log_name, camera_id).get('state', None)

        # Lost communication with camera daemon, this is assumed to be unrecoverable
        # We can return here and let the error be handled when restarting the observation
        if status is None:
            log.error(self.log_name, 'Lost communication with camera ' + camera_id)
            return False

        # Camera may be idle if the pipeline blocked for too long
        if status is CameraStatus.Idle:
            log.warning(self.log_name, f'Found idle camera {camera_id}; restarting')
            return False

        # Power cycling the camera fixes most other errors
        log.warning(self.log_name, f'Camera has timed out in state {CameraStatus.label(status)}; power cycling')
        cam_cycle_power(self.log_name, camera_id)

        time.sleep(5)

        # First initialization after power-on takes longer, so initialize here
        # and allow __observe_field to reinitialize it afterwards
        cam_initialize(self.log_name, camera_id)

        return False

    def __observe_field(self):
        # Start science observations
        pipeline_config = self.config['pipeline'].copy()
        pipeline_config['type'] = 'SCIENCE'

        if not configure_pipeline(self.log_name, pipeline_config):
            return ObservationStatus.Error

        self.set_task('Preparing Cameras')
        if not self.__start_exposures():
            return ObservationStatus.Error

        # The first exposure in a sequence is skipped, so we set the expected exposure start time
        # in the future to avoid false-positive timeouts during the first exposure
        for camera_id in self._camera_ids:
            self._last_exposure_started[camera_id] = Time.now() + self.config[camera_id]['exposure'] * u.s

        # Monitor observation status
        self.set_task(f'Ends {self._end_date.strftime("%H:%M:%S")}')
        return_status = ObservationStatus.Complete
        while True:
            if self.aborted or Time.now() > self._end_date:
                break

            if not self.dome_is_open:
                log.error(self.log_name, 'Aborting because dome is not open')
                return_status = ObservationStatus.DomeClosed
                break

            if not all(self.__check_timeouts(camera_id) for camera_id in self._camera_ids):
                # Try to recover the observation
                return_status = ObservationStatus.OnTarget
                break

            self.wait_until_time_or_aborted(Time.now() + CAM_CHECK_STATUS_DELAY, self._wait_condition)

        # Wait for all cameras to stop before returning to the main loop
        print('ObserveField: stopping science observations')
        cam_stop_synchronised(self.log_name, self._camera_ids)

        return return_status

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Configure pipeline immediately so the dashboard can show target name etc
        if not configure_pipeline(self.log_name, self.config['pipeline'], quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        self.set_task('Waiting for observation start')
        self.wait_until_time_or_aborted(self._start_date, self._wait_condition)
        if Time.now() > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Outer loop handles transitions between states
        # Each method call blocks, returning only when it is ready to exit or switch to a different state
        while True:
            if self._observation_status == ObservationStatus.Error:
                print('ObserveField: status is now Error')
                break

            if self._observation_status == ObservationStatus.Complete:
                print('ObserveField: status is now Complete')
                break

            if self._observation_status == ObservationStatus.OnTarget:
                print('ObserveField: status is now OnTarget')
                self._observation_status = self.__observe_field()

            if self._observation_status == ObservationStatus.PositionLost:
                print('ObserveField: status is now PositionLost')
                self._observation_status = self.__acquire_field()

            if self._observation_status == ObservationStatus.DomeClosed:
                print('ObserveField: status is now DomeClosed')
                self._observation_status = self.__wait_for_dome()

        mount_stop(self.log_name)

        if self._observation_status == ObservationStatus.Complete:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        camera_id = headers.get('CAMID', '').lower()
        print('Got frame from', camera_id, headers['DATE-OBS'], headers['TIME-SRC'])
        self._last_exposure_started[camera_id] = Time(headers['DATE-OBS'], format='isot')

        with self._wait_condition:
            if self._wcs_status == WCSStatus.WaitingForWCS:
                if 'CRVAL1' in headers and 'IMAG-RGN' in headers and 'SITELAT' in headers:
                    r = re.search(r'^\[(\d+):(\d+),(\d+):(\d+)\]$', headers['IMAG-RGN']).groups()
                    cx = (int(r[0]) - 1 + int(r[1])) / 2
                    cy = (int(r[2]) - 1 + int(r[3])) / 2
                    location = EarthLocation(
                        lat=headers['SITELAT'],
                        lon=headers['SITELONG'],
                        height=headers['SITEELEV'])
                    wcs_time = Time(headers['DATE-OBS'], location=location) + 0.5 * headers['EXPTIME'] * u.s
                    self._wcs = wcs.WCS(headers)
                    ra, dec = self._wcs.all_pix2world(cx, cy, 0)
                    self._wcs_field_center = SkyCoord(
                        ra=ra * u.deg,
                        dec=dec * u.deg,
                        frame='icrs',
                        obstime=wcs_time)
                    self._wcs_status = WCSStatus.WCSComplete
                else:
                    self._wcs_status = WCSStatus.WCSFailed
                    self._wcs_field_center = None

                self._wait_condition.notify_all()

    @classmethod
    def config_schema(cls):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['start', 'end', 'pipeline'],
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
                'pipeline': pipeline_science_schema(),
                'onsky': {'type': 'boolean'},  # optional
                'acquisition': {  # optional
                    'type': 'string',
                    'enum': list(cameras.keys())
                }
            }
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema()

        return schema

    @classmethod
    def validate_config(cls, config_json):
        return [jsonschema.exceptions.SchemaError('ObserveFieldBase cannot be scheduled directly')]


class WCSStatus:
    Inactive, WaitingForWCS, WCSFailed, WCSComplete = range(4)


class ObservationStatus:
    PositionLost, OnTarget, DomeClosed, Complete, Error = range(5)
