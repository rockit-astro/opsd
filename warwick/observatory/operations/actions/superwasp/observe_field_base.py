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
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import log
from warwick.observatory.camera.qhy import CameraStatus
from .camera_helpers import cameras, cam_status, cam_take_images, cam_stop
from .mount_helpers import mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_science_schema

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = 60 * u.s

# Amount of time to wait before retrying if an image acquisition generates an error
CAM_ERROR_RETRY_DELAY = 10 * u.s

# Expected time to converge on target field
SETUP_DELAY = 15 * u.s

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
        self._acquisition_camera = config.get('acquisition', None)

        self._wcs_status = WCSStatus.Inactive
        self._wcs = None
        self._wcs_field_center = None

        self._observation_status = ObservationStatus.PositionLost

        self._cameras = {}
        for camera_id in cameras:
            self._cameras[camera_id] = CameraWrapper(camera_id, self.config.get(camera_id, None), self.log_name)

    # pylint: disable=no-self-use
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
    # pylint: enable=no-self-use

    def __acquire_field(self):
        self.set_task('Acquiring field')

        # Point to the requested location
        acquire_start = Time.now()
        print('ObserveField: slewing to target field')
        if not self.slew_to_field():
            return ObservationStatus.Error

        if self._acquisition_camera is None:
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

    def __observe_field(self):
        # Start science observations
        pipeline_config = self.config['pipeline'].copy()

        if not configure_pipeline(self.log_name, pipeline_config):
            return ObservationStatus.Error

        # Mark cameras idle so they will be started by camera.update() below
        print('ObserveField: starting science observations')
        for camera in self._cameras.values():
            if camera.status == CameraWrapperStatus.Stopped:
                camera.status = CameraWrapperStatus.Idle

        # Monitor observation status
        self.set_task(f'Ends {self._end_date.strftime("%H:%M:%S")}')
        return_status = ObservationStatus.Complete
        while True:
            if self.aborted or Time.now() > self._end_date:
                break

            if not self.dome_is_open:
                log.error(self.log_name, 'Aborting because roof is not open')
                return_status = ObservationStatus.DomeClosed
                break

            for camera in self._cameras.values():
                camera.update()
                if camera.status == CameraWrapperStatus.Error:
                    return_status = ObservationStatus.Error
                    break

            self.wait_until_time_or_aborted(Time.now() + CAM_CHECK_STATUS_DELAY, self._wait_condition)

        # Wait for all cameras to stop before returning to the main loop
        print('ObserveField: stopping science observations')
        for camera in self._cameras.values():
            camera.stop()

        while True:
            if all(c.status in [CameraWrapperStatus.Error, CameraWrapperStatus.Stopped]
                    for c in self._cameras.values()):
                break

            for camera in self._cameras.values():
                camera.update()

            with self._wait_condition:
                self._wait_condition.wait(CAM_CHECK_STATUS_DELAY.to_value(u.s))

        print('ObserveField: cameras have stopped')
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
        print('Got frame from', headers.get('CAMID', '').lower())
        camera = self._cameras.get(headers.get('CAMID', '').lower(), None)
        if camera is not None:
            camera.received_frame(headers)

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


class CameraWrapperStatus:
    Idle, Active, Error, Stopping, Stopped, Skipped = range(6)


class CameraWrapper:
    """Holds camera-specific state"""
    def __init__(self, camera_id, camera_config, log_name):
        self.camera_id = camera_id
        self.status = CameraWrapperStatus.Stopped if camera_config is not None else CameraWrapperStatus.Skipped
        self._log_name = log_name
        self._config = camera_config or {}
        self._start_attempts = 0
        self._last_frame_time = Time.now()

    def stop(self):
        if self.status == CameraWrapperStatus.Idle:
            self.status = CameraWrapperStatus.Stopped
        elif self.status == CameraWrapperStatus.Active:
            self.status = CameraWrapperStatus.Stopping
            cam_stop(self._log_name, self.camera_id)

    # pylint: disable=unused-argument
    def received_frame(self, headers):
        """Callback to process an acquired frame. headers is a dictionary of header keys"""
        self._last_frame_time = Time.now()
    # pylint: enable=unused-argument

    def update(self):
        """Monitor camera status"""
        if self.status in [CameraWrapperStatus.Error, CameraWrapperStatus.Stopped, CameraWrapperStatus.Skipped]:
            return

        # Start exposure sequence on first update
        if self.status == CameraWrapperStatus.Idle:
            if cam_take_images(self._log_name, self.camera_id, 0, self._config):
                self._start_attempts = 0
                self._last_frame_time = Time.now()
                self.status = CameraWrapperStatus.Active
                return

            # Something went wrong - see if we can recover
            self._start_attempts += 1
            log.error(self._log_name, 'Failed to start exposures for camera ' + self.camera_id +
                      f' (attempt {self._start_attempts} of 5)')

            if self._start_attempts >= 5:
                log.error(self._log_name, 'Too many start attempts: aborting')
                self.status = CameraWrapperStatus.Error
                return

            # Try stopping the camera and see if we can recover on the next update loop
            cam_stop(self._log_name, self.camera_id)
            return

        if self.status == CameraWrapperStatus.Stopping:
            if cam_status(self._log_name, self.camera_id).get('state', CameraStatus.Idle) == CameraStatus.Idle:
                self.status = CameraWrapperStatus.Stopped
                return

        # Assume that everything is ok if we are still receiving frames at a regular rate
        if Time.now() < self._last_frame_time + self._config['exposure'] * u.s + MAX_PROCESSING_TIME:
            return

        # Exposure has timed out: lets find out why
        status = cam_status(self._log_name, self.camera_id).get('state', None)

        # Lost communication with camera daemon, this is assumed to be unrecoverable
        if status is None:
            log.error(self._log_name, 'Lost communication with camera ' + self.camera_id)
            self.status = CameraWrapperStatus.Error
            return

        # Camera may be idle if the pipeline blocked for too long
        if status is CameraStatus.Idle:
            log.warning(self._log_name, 'Recovering idle camera ' + self.camera_id)
            self.status = CameraWrapperStatus.Idle
            self.update()
            return

        # Try stopping the camera and see if we can recover on the next update loop
        log.warning(self._log_name, f'Camera has timed out in state {CameraStatus.label(status)}, stopping camera')
        cam_stop(self._log_name, self.camera_id)
