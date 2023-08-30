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

"""Telescope action to observe a sidereally tracked field"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

from collections import deque
import re
import sys
import threading
import time
import traceback

from astropy import wcs
from astropy.wcs.utils import local_partial_pixel_derivatives
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time
import astropy.units as u
import numpy as np
from scipy import conjugate, polyfit
from scipy.fftpack import fft, ifft
from rockit.common import log, validation
from warwick.observatory.camera.qhy import CameraStatus
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cam_status, cam_take_images, cam_stop
from .mount_helpers import mount_slew_radec, mount_offset_radec, mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_science_schema

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = 25 * u.s

# Amount of time to wait before retrying if an image acquisition generates an error
CAM_ERROR_RETRY_DELAY = 10 * u.s

# Exposure time to use when taking a WCS field image
WCS_EXPOSURE_TIME = 5 * u.s

# Amount of time to wait between camera status checks while observing
CAM_CHECK_STATUS_DELAY = 10 * u.s


# Track a limited history of shifts so we can handle outliers
GUIDE_BUFFER_REJECTION_SIGMA = 10
GUIDE_BUFFER_LENGTH = 20

# Shifts larger than this are automatically rejected without touching the guide buffer
GUIDE_MAX_PIXEL_ERROR = 100

# PID loop coefficients
GUIDE_PID = [0.75, 0.02, 0.0]


class ObserveField(TelescopeAction):
    """Telescope action to observe a sidereally tracked field"""
    def __init__(self, log_name, config):
        super().__init__('Observe Field', log_name, config)
        self._wait_condition = threading.Condition()

        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])

        self._wcs_status = WCSStatus.Inactive
        self._wcs = None
        self._wcs_field_center = None
        self._wcs_derivatives = None

        self._observation_status = ObservationStatus.PositionLost
        self._is_guiding = False
        self._guide_profiles = None

        self._camera = CameraWrapper(self.config.get('camera', None), self.log_name)

        self._guide_buff_x = deque(maxlen=GUIDE_BUFFER_LENGTH)
        self._guide_buff_y = deque(maxlen=GUIDE_BUFFER_LENGTH)
        self._guide_pid_x = PIDController(*GUIDE_PID)
        self._guide_pid_y = PIDController(*GUIDE_PID)

    def __acquire_field(self):
        self.set_task('Acquiring field')

        # Point to the requested location
        acquire_start = Time.now()
        print('ObserveField: slewing to target field')
        blind_offset_dra = self.config.get('blind_offset_dra', 0)
        blind_offset_ddec = self.config.get('blind_offset_ddec', 0)
        acquisition_ra = self.config['ra'] + blind_offset_dra
        acquisition_dec = self.config['dec'] + blind_offset_ddec
        if not mount_slew_radec(self.log_name, acquisition_ra, acquisition_dec, True):
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
        cam_config.update(self.config.get('camera', {}))
        cam_config.update({
            'exposure': WCS_EXPOSURE_TIME.to(u.second).value
        })

        # Converge on requested position
        attempt = 1
        target = SkyCoord(ra=acquisition_ra,
                          dec=acquisition_dec,
                          unit=u.degree, frame='icrs')
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
            while not cam_take_images(self.log_name, 1, cam_config, quiet=True):
                # Try stopping the camera, waiting a bit, then try again
                cam_stop(self.log_name)
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
            # TODO: Unhardcode the pointing threshold
            if abs(offset_ra) < 5 * u.arcsecond and abs(offset_dec) < 5 * u.arcsecond:
                dt = (Time.now() - acquire_start).to(u.s).value
                print(f'ObserveField: Acquired field in {dt:.1f} seconds')
                if blind_offset_dra != 0 or blind_offset_ddec != 0:
                    print('ObserveField: Offsetting to target')
                    if not mount_offset_radec(self.log_name, -blind_offset_dra, -blind_offset_ddec):
                        return ObservationStatus.Error

                    # Wait for the offset to complete
                    # TODO: monitor tel status instead!
                    time.sleep(10)

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
        pipeline_config['guide'] = 'HALFMETRE'
        pipeline_config['type'] = 'SCIENCE'
        pipeline_config['archive'] = ['HALFMETRE']

        if not configure_pipeline(self.log_name, pipeline_config):
            return ObservationStatus.Error

        # Mark cameras idle so they will be started by camera.update() below
        print('ObserveField: starting science observations')
        if self._camera.status == CameraWrapperStatus.Stopped:
            self._camera.status = CameraWrapperStatus.Idle

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

            self._camera.update()
            if self._camera.status == CameraWrapperStatus.Error:
                return_status = ObservationStatus.Error
                break

            self.wait_until_time_or_aborted(Time.now() + CAM_CHECK_STATUS_DELAY, self._wait_condition)

        # Wait for all cameras to stop before returning to the main loop
        print('ObserveField: stopping science observations')
        self._is_guiding = False
        self._camera.stop()

        while True:
            if self._camera.status in [CameraWrapperStatus.Error, CameraWrapperStatus.Stopped]:
                break

            self._camera.update()

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
        print('Got frame')
        self._camera.received_frame(headers)

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
                    self._wcs_derivatives = local_partial_pixel_derivatives(self._wcs, cx, cy)
                    self._wcs_status = WCSStatus.WCSComplete
                else:
                    self._wcs_status = WCSStatus.WCSFailed
                    self._wcs_field_center = None

                self._wait_condition.notify_all()

    def received_guide_profile(self, headers, profile_x, profile_y):
        """Notification called when a guide profile has been calculated by the data pipeline"""
        if not self._is_guiding:
            return

        if self._guide_profiles is None:
            print('ObserveField: set reference guide profiles')
            self._guide_profiles = profile_x, profile_y
            return

        try:
            # Measure image offset
            dx = cross_correlate(profile_x, self._guide_profiles[0])
            dy = cross_correlate(profile_y, self._guide_profiles[1])
            print(f'ObserveField: measured guide offsets {dx:.2f} {dy:.2f} px')

            # Ignore suspiciously big shifts
            if abs(dx) > GUIDE_MAX_PIXEL_ERROR or abs(dy) > GUIDE_MAX_PIXEL_ERROR:
                print(f'ObserveField: Offset larger than max allowed pixel shift: x: {dx} y:{dy}')
                print('ObserveField: Skipping this correction')
                return

            # Store the pre-pid values in the buffer
            self._guide_buff_x.append(dx)
            self._guide_buff_y.append(dx)

            # Ignore shifts that are inconsistent with previous shifts,
            # but only after we have collected enough measurements to trust the stats
            if len(self._guide_buff_x) == self._guide_buff_x.maxlen:
                if abs(dx) > GUIDE_BUFFER_REJECTION_SIGMA * np.std(self._guide_buff_x) or \
                        abs(dy) > GUIDE_BUFFER_REJECTION_SIGMA * np.std(self._guide_buff_y):
                    print(f'ObserveField: Guide correction(s) too large x:{dx:.2f} y:{dy:.2f}')
                    print('ObserveField: Skipping this correction but adding to stats buffer')
                    return

            # Generate the corrections from the PID controllers
            corr_dx = -self._guide_pid_x.update(dx)
            corr_dy = -self._guide_pid_y.update(dy)
            print(f'ObserveField: post-PID corrections {corr_dx:.2f} {corr_dy:.2f} px')

            corr_dra = self._wcs_derivatives[0, 0] * corr_dx + self._wcs_derivatives[0, 1] * corr_dy
            corr_ddec = self._wcs_derivatives[1, 0] * corr_dx + self._wcs_derivatives[1, 1] * corr_dy
            print(f'ObserveField: post-PID corrections {corr_dra * 3600:.2f} {corr_ddec * 3600:.2f} arcsec')

            # TODO: reacquire using WCS (self._is_guiding = False) if we detect things have gone wrong

            # Apply correction
            mount_offset_radec(self.log_name, corr_dra, corr_ddec)
        except Exception:
            traceback.print_exc(file=sys.stdout)
            self._is_guiding = False

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['start', 'end', 'ra', 'dec', 'pipeline', 'camera'],
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
                'blind_offset_dra': {
                    'type': 'number'
                },
                'blind_offset_ddec': {
                    'type': 'number'
                },
                'pipeline': pipeline_science_schema(),
                'camera': camera_science_schema()
            }
        }

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

class PIDController:
    """
    Simple PID controller that acts to minimise the given error term.
    Note that this assumes that frames are coming in with an equal cadence,
    which allows us to ignore the delta-time handling from the loop
    """

    def __init__(self, kp, ki, kd, max_integrated_error=500):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.max_integrated_error = max_integrated_error

        self.previous_error = 0
        self.integral = 0
        self.derivative = 0

    def update(self, error):
        # Reduce the impact of "windup error" by bounding the integral within a maximum range
        self.integral = max(min(self.integral + error, self.max_integrated_error), -self.max_integrated_error)
        self.derivative = error - self.previous_error
        self.previous_error = error
        return self.kp * error + self.ki * self.integral + self.kd * (error - self.previous_error)


class WCSStatus:
    Inactive, WaitingForWCS, WCSFailed, WCSComplete = range(4)


class ObservationStatus:
    PositionLost, OnTarget, DomeClosed, Complete, Error = range(5)


class CameraWrapperStatus:
    Idle, Active, Error, Stopping, Stopped, Skipped = range(6)


class CameraWrapper:
    """Holds camera-specific flat state"""
    def __init__(self, camera_config, log_name):
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
            cam_stop(self._log_name)

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
            if cam_take_images(self._log_name, 0, self._config):
                self._start_attempts = 0
                self._last_frame_time = Time.now()
                self.status = CameraWrapperStatus.Active
                return

            # Something went wrong - see if we can recover
            self._start_attempts += 1
            log.error(self._log_name, f'Failed to start exposures (attempt {self._start_attempts} of 5)')

            if self._start_attempts >= 5:
                log.error(self._log_name, 'Too many start attempts: aborting')
                self.status = CameraWrapperStatus.Error
                return

            # Try stopping the camera and see if we can recover on the next update loop
            cam_stop(self._log_name)
            return

        if self.status == CameraWrapperStatus.Stopping:
            if cam_status(self._log_name).get('state', CameraStatus.Idle) == CameraStatus.Idle:
                self.status = CameraWrapperStatus.Stopped
                return

        # Assume that everything is ok if we are still receiving frames at a regular rate
        if Time.now() < self._last_frame_time + self._config['exposure'] * u.s + MAX_PROCESSING_TIME:
            return

        # Exposure has timed out: lets find out why
        status = cam_status(self._log_name).get('state', None)

        # Lost communication with camera daemon, this is assumed to be unrecoverable
        if status is None:
            log.error(self._log_name, 'Lost communication with camera')
            self.status = CameraWrapperStatus.Error
            return

        # Camera may be idle if the pipeline blocked for too long
        if status is CameraStatus.Idle:
            log.warning(self._log_name, 'Recovering idle camera')
            self.status = CameraWrapperStatus.Idle
            self.update()
            return

        # Try stopping the camera and see if we can recover on the next update loop
        log.warning(self._log_name, f'Camera has timed out in state {CameraStatus.label(status)}, stopping camera')
        cam_stop(self._log_name)