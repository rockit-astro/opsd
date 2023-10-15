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

"""Helper functions for acquiring a field using WCS"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import re
import threading
import time
from astropy.coordinates import SkyCoord
from astropy.time import Time
import astropy.units as u
from astropy.wcs import WCS
from astropy.wcs.utils import local_partial_pixel_derivatives
import numpy as np

# pylint: disable=no-name-in-module
from scipy import conjugate, polyfit
from scipy.fftpack import fft, ifft
# pylint: enable=no-name-in-module

from rockit.camera.qhy import CameraStatus
from rockit.common import log

from .camera_helpers import cam_status, cam_stop, cam_take_images
from .mount_helpers import mount_offset_radec, mount_slew_radec, mount_sync
from .pipeline_helpers import configure_pipeline

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = 25 * u.s

# Amount of time to wait before retrying if an image acquisition generates an error
CAM_ERROR_RETRY_DELAY = 10 * u.s

# Exposure time to use when taking a WCS field image
WCS_EXPOSURE_TIME = 5 * u.s

# Amount of time to wait between camera status checks while observing
CAM_CHECK_STATUS_DELAY = 10 * u.s


class WCSStatus:
    Inactive, WaitingForWCS, WCSFailed, WCSComplete = range(4)


class CameraWrapperStatus:
    Idle, Active, Error, Stopping, Stopped = range(5)


class FieldAcquisitionHelper:
    def __init__(self, parent_action):
        self._wait_condition = threading.Condition()
        self._parent_action = parent_action
        self._wcs_status = WCSStatus.Inactive

        self.wcs_field_center = None
        self.wcs_derivatives = None

    def acquire_field(self, ra_degrees, dec_degrees, threshold_arcsec=5):
        acquire_start = Time.now()
        if not mount_slew_radec(self._parent_action.log_name, ra_degrees, dec_degrees, True):
            return False

        # Take a frame to solve field center
        pipeline_config = {
            'wcs': True,
            'type': 'JUNK',
            'object': 'WCS',
        }

        if not configure_pipeline(self._parent_action.log_name, pipeline_config, quiet=True):
            return False

        # Converge on requested position
        attempt = 1
        target = SkyCoord(ra=ra_degrees,
                          dec=dec_degrees,
                          unit=u.degree, frame='icrs')

        while not self._parent_action.aborted and self._parent_action.dome_is_open:
            # Wait for telescope position to settle before taking first image
            time.sleep(5)

            self._wcs_status = WCSStatus.WaitingForWCS

            print('FieldAcquisitionHelper: taking test image')
            camera_config = {
                'exposure': 5,
                'stream': False
            }

            while not cam_take_images(self._parent_action.log_name, config=camera_config, quiet=True):
                # Try stopping the camera, waiting a bit, then try again
                cam_stop(self._parent_action.log_name)
                time.sleep(10)
                if self._parent_action.aborted or not self._parent_action.dome_is_open:
                    break

                attempt += 1
                if attempt == 6:
                    return False

            if self._parent_action.aborted or not self._parent_action.dome_is_open:
                break

            # Wait for new frame
            expected_complete = Time.now() + camera_config['exposure'] * u.s + MAX_PROCESSING_TIME

            while True:
                with self._wait_condition:
                    remaining = (expected_complete - Time.now()).to(u.second).value
                    if remaining < 0 or self._wcs_status != WCSStatus.WaitingForWCS:
                        break

                    self._wait_condition.wait(max(remaining, 1))

            if self._parent_action.aborted or not self._parent_action.dome_is_open:
                break

            failed = self._wcs_status == WCSStatus.WCSFailed
            timeout = self._wcs_status == WCSStatus.WaitingForWCS
            self._wcs_status = WCSStatus.Inactive

            if failed or timeout:
                if failed:
                    print('FieldAcquisitionHelper: WCS failed for attempt', attempt)
                else:
                    print('FieldAcquisitionHelper: WCS timed out for attempt', attempt)

                attempt += 1
                if attempt == 6:
                    return False

                continue

            # Calculate frame center and offset from expected pointing
            offset_ra, offset_dec = self.wcs_field_center.spherical_offsets_to(target)
            offset = self.wcs_field_center.separation(target)

            print(f'FieldAcquisitionHelper: offset is {offset_ra.to_value(u.arcsecond):.1f}, ' +
                  f'{offset_dec.to_value(u.arcsecond):.1f}')

            # Close enough!
            if offset < threshold_arcsec * u.arcsecond:
                dt = (Time.now() - acquire_start).to(u.s).value
                print(f'FieldAcquisitionHelper: Acquired field in {dt:.1f} seconds')
                return True

            # Sync and repoint if offset is large
            if offset >= 1 * u.arcminute:
                actual_ra = self.wcs_field_center.ra.to_value(u.deg)
                actual_dec = self.wcs_field_center.dec.to_value(u.deg)
                if not mount_sync(self._parent_action.log_name, actual_ra, actual_dec):
                    return False

                if not mount_slew_radec(self._parent_action.log_name, ra_degrees, dec_degrees, True):
                    return False

            # Offset telescope
            elif not mount_offset_radec(self._parent_action.log_name,
                                        offset_ra.to_value(u.deg), offset_dec.to_value(u.deg)):
                return False

        return True

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        with self._wait_condition:
            if self._wcs_status == WCSStatus.WaitingForWCS:
                if 'CRVAL1' in headers and 'IMAG-RGN' in headers and 'SITELAT' in headers:
                    r = re.search(r'^\[(\d+):(\d+),(\d+):(\d+)\]$', headers['IMAG-RGN']).groups()
                    cx = (int(r[0]) - 1 + int(r[1])) / 2
                    cy = (int(r[2]) - 1 + int(r[3])) / 2

                    wcs = WCS(headers)
                    ra, dec = wcs.all_pix2world(cx, cy, 0)
                    self.wcs_field_center = SkyCoord(ra=ra, dec=dec, unit=u.deg, frame='icrs')
                    self.wcs_derivatives = local_partial_pixel_derivatives(wcs, cx, cy)
                    self._wcs_status = WCSStatus.WCSComplete
                else:
                    self._wcs_status = WCSStatus.WCSFailed
                    self.wcs_field_center = None

                self._wait_condition.notify_all()

    def aborted_or_dome_status_changed(self):
        with self._wait_condition:
            self._wait_condition.notify_all()


class CameraWrapper:
    """Holds camera-specific state"""
    def __init__(self, parent_action):
        self.status = CameraWrapperStatus.Stopped
        self.completed_frames = 0
        self.target_frames = 0
        self._log_name = parent_action.log_name
        self._config = {}
        self._start_attempts = 0
        self._last_frame_time = Time.now()

    def stop(self):
        if self.status == CameraWrapperStatus.Idle:
            self.status = CameraWrapperStatus.Stopped
        elif self.status == CameraWrapperStatus.Active:
            self.status = CameraWrapperStatus.Stopping
            cam_stop(self._log_name)

    def start(self, config, total=0):
        self._start_attempts = 0
        self._last_frame_time = Time.now()
        self._config = config
        self.completed_frames = 0
        self.target_frames = total
        if self.status == CameraWrapperStatus.Stopped:
            self.status = CameraWrapperStatus.Idle

        self.update()

    # pylint: disable=unused-argument
    def received_frame(self, headers):
        """Callback to process an acquired frame. headers is a dictionary of header keys"""
        self._last_frame_time = Time.now()
        self.completed_frames += 1
    # pylint: enable=unused-argument

    def update(self):
        """Monitor camera status"""
        if self.status in [CameraWrapperStatus.Error, CameraWrapperStatus.Stopped]:
            return

        if self.status in [CameraWrapperStatus.Idle, CameraWrapperStatus.Active] and self.target_frames != 0 and \
                self.completed_frames == self.target_frames:
            self.status = CameraWrapperStatus.Stopped

        # Start exposure sequence on first update
        if self.status == CameraWrapperStatus.Idle:
            if cam_take_images(self._log_name, self.target_frames - self.completed_frames, self._config):
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
        if Time.now() < self._last_frame_time + 2 * self._config['exposure'] * u.s + MAX_PROCESSING_TIME:
            return

        # Exposure has timed out: lets find out why
        status = cam_status(self._log_name).get('state', None)

        # Lost communication with camera daemon, this is assumed to be unrecoverable
        if not status:
            log.error(self._log_name, 'Lost communication with camera')
            self.status = CameraWrapperStatus.Error
            return

        # Camera may be idle if the pipeline blocked for too long
        if status == CameraStatus.Idle:
            log.warning(self._log_name, 'Recovering idle camera')
            self.status = CameraWrapperStatus.Idle
            self.update()
            return

        # Try stopping the camera and see if we can recover on the next update loop
        log.warning(self._log_name, f'Camera has timed out in state {CameraStatus.label(status)}, stopping camera')
        cam_stop(self._log_name)


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
