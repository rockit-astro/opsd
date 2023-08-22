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
# pylint: disable=too-many-branches

import re
import threading
import time
from astropy import wcs
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time
import astropy.units as u
import numpy as np
from scipy import conjugate, polyfit
from scipy.fftpack import fft, ifft
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from rockit.common import log, validation
from warwick.observatory.camera.andor import CameraStatus
from .camera_helpers import cameras, cam_status, cam_take_images, cam_stop
from .mount_helpers import mount_slew_radec, mount_offset_radec, mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_science_schema

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = 25 * u.s

# Amount of time to wait before retrying if an image acquisition generates an error
CAM_ERROR_RETRY_DELAY = 10 * u.s

# Expected time to converge on target field
SETUP_DELAY = 15 * u.s

# Exposure time to use when taking a WCS field image
WCS_EXPOSURE_TIME = 5 * u.s

# Amount of time to wait between camera status checks while observing
CAM_CHECK_STATUS_DELAY = 10 * u.s

class ObserveField(TelescopeAction):
    """Telescope action to observe a sidereally tracked field"""
    def __init__(self, log_name, config):
        super().__init__('Observe Field', log_name, config)
        self._wait_condition = threading.Condition()

        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])
        self._guide_camera = config['guide_camera']

        self._wcs_status = WCSStatus.Inactive
        self._wcs = None
        self._wcs_field_center = None

        self._observation_status = ObservationStatus.PositionLost
        self._is_guiding = False
        self._guide_profiles = None

        self._cameras = {}
        for camera_id in cameras:
            self._cameras[camera_id] = CameraWrapper(camera_id, self.config.get(camera_id, None), self.log_name)

    def __acquire_field(self):
        self.set_task('Acquiring field')

        # Point to the requested location
        acquire_start = Time.now()
        print('ObserveField: slewing to target field')
        if not mount_slew_radec(self.log_name, self.config['ra'], self.config['dec'], True):
            return ObservationStatus.Error

        # Take a frame to solve field center
        pipeline_config = {
            'wcs': True,
            'type': 'JUNK',
            'object': 'WCS',
        }

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            return ObservationStatus.Error

        cam_config = {}
        cam_config.update(self.config.get(self._guide_camera, {}))
        cam_config.update({
            'exposure': WCS_EXPOSURE_TIME.to(u.second).value,
            'shutter': True
        })

        # Converge on requested position
        attempt = 1
        target = SkyCoord(self.config['ra'], self.config['dec'], unit=u.degree, frame='icrs')
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
            while not cam_take_images(self.log_name, self._guide_camera, 1, cam_config, quiet=True):
                # Try stopping the camera, waiting a bit, then try again
                cam_stop(self.log_name, self._guide_camera)
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
                    remaining = (expected_complete - Time.now()).to(u.second).value
                    if remaining < 0 or self._wcs_status != WCSStatus.WaitingForWCS:
                        break

                    self._wait_condition.wait(max(remaining, 1))

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

            # Calculate frame center and offset from expected pointing
            actual = SkyCoord(self._wcs_field_center.ra, self._wcs_field_center.dec, frame='icrs')
            offset_ra, offset_dec = actual.spherical_offsets_to(target)

            print(f'ObserveField: offset is {offset_ra.to_value(u.arcsecond):.1f}, ' +
                  f'{offset_dec.to_value(u.arcsecond):.1f}')

            # Close enough!
            if offset_ra < 5 * u.arcsecond and offset_dec < 5 * u.arcsecond:
                dt = (Time.now() - acquire_start).to(u.s).value
                print(f'ObserveField: Acquired field in {dt:.1f} seconds')
                return ObservationStatus.OnTarget

            # Offset telescope
            self.set_task('Refining pointing')
            if not mount_offset_radec(self.log_name, offset_ra.to_value(u.deg), offset_dec.to_value(u.deg)):
                return ObservationStatus.Error

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
        pipeline_config['guide'] = self._guide_camera.upper()
        pipeline_config['type'] = 'SCIENCE'

        if not configure_pipeline(self.log_name, pipeline_config):
            return ObservationStatus.Error

        # Mark cameras idle so they will be started by camera.update() below
        print('ObserveField: starting science observations')
        for camera in self._cameras.values():
            if camera.status == CameraWrapperStatus.Stopped:
                camera.status = CameraWrapperStatus.Idle

        self._is_guiding = True

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

            if not self._is_guiding:
                log.warning(self.log_name, 'Lost autoguiding lock')
                return_status = ObservationStatus.PositionLost
                break

            for camera in self._cameras.values():
                camera.update()
                if camera.status == CameraWrapperStatus.Error:
                    return_status = ObservationStatus.Error
                    break

            self.wait_until_time_or_aborted(Time.now() + CAM_CHECK_STATUS_DELAY, self._wait_condition)

        # Wait for all cameras to stop before returning to the main loop
        print('ObserveField: stopping science observations')
        self._is_guiding = False
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

    def received_guide_profile(self, headers, profile_x, profile_y):
        """Notification called when a guide profile has been calculated by the data pipeline"""
        camera = headers.get('CAMID', '').lower()
        if camera != self._guide_camera or not self._is_guiding:
            return

        if self._guide_profiles is None:
            print('ObserveField: set reference guide profiles')
            self._guide_profiles = profile_x, profile_y
            return

        # Measure image offset
        dx = cross_correlate(profile_x, self._guide_profiles[0])
        dy = cross_correlate(profile_y, self._guide_profiles[1])

        # TODO: Use WCS matrix to convert pixel offsets to sky offsets
        print(f'ObserveField: measured guide offsets {dx:.2f} {dy:.2f} px')

        # Stop science observations and reacquire using WCS if we are too far off target
        # TODO: Do this in arcseconds
        if abs(dx) > 100 or abs(dy) > 100:
            self._is_guiding = False
            return

        # TODO: Apply guide offset

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['start', 'end', 'ra', 'dec', 'guide_camera', 'pipeline'],
            'properties': {
                'type': {'type': 'string'},
                'start': {
                    'type': 'string',
                    'format': 'date-time'
                },
                'end': {
                    'type': 'string',
                    'format': 'date-time'
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
                'guide_camera': {
                    'type': 'string',
                    'enum': list(cameras.keys())
                },
                'pipeline': pipeline_science_schema()
            }
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema(camera_id)

        return validation.validation_errors(config_json, schema)


def cross_correlate(check, reference):
    corr = ifft(conjugate(fft(reference)) * fft(check))
    peak = np.argmax(corr)

    # Fit sub-pixel offset using a quadratic fit over the 3 pixels centered on the peak
    if peak == len(corr) - 1:
        x = [-1, 0, 1]
        y = [
            corr[-2].real,
            corr[-1].real,
            corr[0].real
        ]
        coeffs = polyfit(x, y, 2)
        return 1 + (coeffs[1] / (2 * coeffs[0]))

    if peak == 0:
        x = [1, 0, -1]
        y = [
            corr[-1].real,
            corr[0].real,
            corr[1].real,
        ]
        coeffs = polyfit(x, y, 2)
        return -coeffs[1] / (2 * coeffs[0])

    x = [peak - 1, peak, peak + 1]
    y = [
        corr[x[0]].real,
        corr[x[1]].real,
        corr[x[2]].real
    ]
    coeffs = polyfit(x, y, 2)
    if peak <= len(corr) / 2:
        return -(-coeffs[1] / (2 * coeffs[0]))
    return len(corr) + (coeffs[1] / (2 * coeffs[0]))


class WCSStatus:
    Inactive, WaitingForWCS, WCSFailed, WCSComplete = range(4)


class ObservationStatus:
    PositionLost, OnTarget, DomeClosed, Complete, Error = range(5)


class CameraWrapperStatus:
    Idle, Active, Error, Stopping, Stopped, Skipped = range(6)


class CameraWrapper:
    """Holds camera-specific flat state"""
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
