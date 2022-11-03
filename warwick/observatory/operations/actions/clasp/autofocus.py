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

"""Telescope action to find focus using the v-curve technique"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import threading
import numpy as np
from astropy.time import Time
import astropy.units as u
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import log, validation
from .mount_helpers import mount_slew_radec, mount_status, mount_stop
from .focus_helpers import focus_set, focus_get
from .camera_helpers import cameras, cam_configure, cam_take_images
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema


class AutoFocus(TelescopeAction):
    """
    Telescope action to find focus using the v-curve technique

    Example block:
    {
        "type": "AutoFocus",
        "start": "2022-09-18T22:20:00", # Optional: defaults to immediately
        "expires": "2022-09-18T22:30:00", # Optional: defaults to never
        "ra": 0, # Optional: defaults to zenith
        "dec": -4.5, # Optional: defaults to zenith
        "cam<1..2>": { # Optional: cameras that aren't listed won't be focused
            "exposure": 1
            # Also supports optional temperature, window, gain, offset, stream (advanced options)
        }
    }
    """
    def __init__(self, log_name, config):
        super().__init__('Auto Focus', log_name, config)
        self._wait_condition = threading.Condition()

        if 'start' in config:
            self._start_date = Time(config['start'])
        else:
            self._start_date = None

        if 'expires' in config:
            self._expires_date = Time(config['expires'])
        else:
            self._expires_date = None

        self._cameras = {}
        for camera_id in cameras:
            self._cameras[camera_id] = CameraWrapper(camera_id, CONFIG, self.config.get(camera_id, None), self.log_name)

    def run_thread(self):
        """Thread that runs the hardware actions"""
        if self._start_date is not None and Time.now() < self._start_date:
            self.set_task(f'Waiting until {self._start_date.strftime("%H:%M:%S")}')
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        while not self.aborted and not self.dome_is_open:
            if self._expires_date is not None and Time.now() > self._expires_date:
                break

            self.set_task('Waiting for dome')
            with self._wait_condition:
                self._wait_condition.wait(10)

        if self.aborted or self._expires_date is not None and Time.now() > self._expires_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Fall back to zenith if coords not specified
        ra = self.config.get('ra', None)
        dec = self.config.get('dec', None)
        if ra is None or dec is None:
            ms = mount_status(self.log_name)
            if ms is None or 'lst' not in ms or 'site_latitude' not in ms:
                log.error(self.log_name, 'Failed to query mount LST or latitude')
                self.status = TelescopeActionStatus.Error
                return

            if ra is None:
                ra = ms['lst']

            if dec is None:
                dec = ms['site_latitude']

        self.set_task('Slewing to field')
        if not mount_slew_radec(self.log_name, ra, dec, True):
            self.status = TelescopeActionStatus.Error
            return

        self.set_task('Preparing cameras')

        pipeline_config = {
            'hfd': True,
            'type': 'JUNK'
        }

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        # This starts the autofocus logic, which is run
        # in the received_frame callbacks
        for camera in self._cameras.values():
            camera.start()

        # Wait until complete
        while True:
            with self._wait_condition:
                self._wait_condition.wait(5)

            codes = ''
            for camera in self._cameras.values():
                camera.check_timeout()
                codes += AutoFocusState.Codes[camera.state]

            self.set_task('Focusing (' + ''.join(codes) + ')')
            if self.aborted:
                break

            if not self.dome_is_open:
                for camera in self._cameras.values():
                    camera.abort()

                log.error(self.log_name, 'AutoFocus: Dome has closed')
                break

            # We are done once all cameras are either complete or have errored
            if all(camera.state >= AutoFocusState.Complete for camera in self._cameras.values()):
                break

        if any(camera.state == AutoFocusState.Error for camera in self._cameras.values()):
            self.status = TelescopeActionStatus.Error
        else:
            self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        mount_stop(self.log_name)
        for camera in self._cameras.values():
            camera.abort()

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
        if camera_id in self._cameras:
            self._cameras[camera_id].received_frame(headers)
        else:
            print('AutoFocus: Ignoring unknown frame')

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': [],
            'properties': {
                'type': {'type': 'string'},
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
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'expires': {
                    'type': 'string',
                    'format': 'date-time',
                }
            }
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema()

        return validation.validation_errors(config_json, schema)


class AutoFocusState:
    """Possible states of the AutoFlat routine"""
    MeasureInitial, FindPositionOnVCurve, FindTargetHFD, MeasureTargetHFD, \
        MeasureFinalHFD, Aborting, Complete, Failed, Error = range(9)

    Codes = ['I', 'V', 'T', 'M', 'N', 'A', 'C', 'F', 'E']


class CameraWrapper:
    """Holds camera-specific focus state"""
    def __init__(self, camera_id, config, camera_config, log_name):
        self.camera_id = camera_id
        if camera_config is not None:
            self.state = AutoFocusState.MeasureInitial
        else:
            self.state = AutoFocusState.Complete

        self._log_name = log_name
        self._config = config
        self._camera_config = camera_config
        self._start_time = None
        self._expected_complete = None
        self._initial_focus = None
        self._current_focus = None
        self._measurements = []
        self._failed_measurements = 0
        self._best_hfd = None

    def start(self):
        """Starts the autofocus sequence for this camera"""
        if self.state == AutoFocusState.Complete:
            return

        self._start_time = Time.now()

        # Record the initial focus so we can return on error
        self._initial_focus = self._current_focus = focus_get(self._log_name, self.camera_id)
        if self._initial_focus is None:
            self.state = AutoFocusState.Error
            return

        # Set the camera config once at the start to avoid duplicate changes
        cam_config = self._camera_config.copy()
        cam_config['stream'] = False

        if not cam_configure(self._log_name, self.camera_id, cam_config):
            self.state = AutoFocusState.Error
            return

        # Take the first image to start the process.
        # The main state machine runs inside received_frame.
        self._take_image()

    def _set_failed(self):
        """Restores the original focus position and marks state as failed"""
        if not focus_set(self._log_name, self.camera_id, self._initial_focus):
            log.error(self._log_name, f'AutoFocus: camera {self.camera_id} failed to restore initial focus')
            self.state = AutoFocusState.Error
        else:
            self.state = AutoFocusState.Failed

    def _set_error(self):
        """Restores the original focus position and marks state as error"""
        if not focus_set(self._log_name, self.camera_id, self._initial_focus):
            log.error(self._log_name, f'AutoFocus: camera {self.camera_id} failed to restore initial focus')
        self.state = AutoFocusState.Error

    def _take_image(self):
        """Tells the camera to take an exposure."""
        timeout = (self._camera_config['exposure'] + self._config['max_processing_time']) * u.s
        self._expected_complete = Time.now() + timeout

        if not cam_take_images(self._log_name, self.camera_id, quiet=True):
            self._set_error()

    def check_timeout(self):
        """Sets error state if an expected frame is more than 30 seconds late"""
        if self.state > AutoFocusState.Aborting:
            return

        if self._expected_complete and Time.now() > self._expected_complete:
            log.error(self._log_name, f'AutoFocus: camera {self.camera_id} exposure timed out')
            self._set_error()

    def received_frame(self, headers):
        """Callback to process an acquired frame. headers is a dictionary of header keys"""
        if self.state >= AutoFocusState.Complete:
            return

        if self.state == AutoFocusState.Aborting:
            self._set_failed()
            return

        if 'MEDHFD' not in headers or 'HFDCNT' not in headers:
            log.warning(self._log_name, f'AutoFocus: camera {self.camera_id} discarding frame without HFD headers')
            self._failed_measurements += 1
            if self._failed_measurements == 5:
                log.error(self._log_name, f'AutoFocus: camera {self.camera_id} aborting because 5 HFD samples failed')
                self._set_failed()
                return
        else:
            hfd = headers['MEDHFD']
            count = headers['HFDCNT']
            if count > self._config['minimum_object_count'] and hfd > self._config['minimum_hfd']:
                self._measurements.append(hfd)
            else:
                log.warning(self._log_name,
                            f'AutoFocus: camera {self.camera_id} discarding frame with {count} samples ({hfd} HFD)')
                self._failed_measurements += 1
                if self._failed_measurements == 5:
                    log.error(self._log_name,
                              f'AutoFocus: camera {self.camera_id} aborting because 5 HFD samples failed')
                    self._set_failed()
                    return

        requested = self._config['coarse_measure_repeats']
        if self.state in [AutoFocusState.MeasureTargetHFD, AutoFocusState.MeasureFinalHFD]:
            requested = self._config['fine_measure_repeats']

        if len(self._measurements) == requested:
            print(self.camera_id, ' hfd values:', self._measurements)
            current_hfd = float(np.min(self._measurements))
            log.info(self._log_name,
                     f'AutoFocus: camera {self.camera_id} HFD at {self._current_focus} steps is {current_hfd:.1f}" ' +
                     f'({requested} samples)')

            self._measurements.clear()
            self._failed_measurements = 0

            if self.state == AutoFocusState.MeasureInitial:
                self.state = AutoFocusState.FindPositionOnVCurve

            if self.state == AutoFocusState.FindPositionOnVCurve:
                # Step inwards until we are well defocused on the inside edge of the v curve
                if self._best_hfd is not None and current_hfd > 2 * self._best_hfd and \
                        current_hfd > self._config['target_hfd']:
                    log.info(self._log_name, f'AutoFocus: camera {self.camera_id} found position on v-curve')
                    self.state = AutoFocusState.FindTargetHFD
                else:
                    self._current_focus -= self._config['focus_step_size']
                    if not focus_set(self._log_name, self.camera_id, self._current_focus):
                        self._set_error()
                        return

            # Note: not an elif to allow the FindPositionOnVCurve case above to enter this branch too
            if self.state == AutoFocusState.FindTargetHFD:
                # We may have stepped to far inwards in the previous step
                # Step outwards if needed until the current HFD is closer to the target
                if current_hfd > 2 * self._config['target_hfd']:
                    log.info(self._log_name,
                             f'AutoFocus: camera {self.camera_id} stepping towards HFD {self._config["target_hfd"]}')

                    self._current_focus -= int(current_hfd / (2 * self._config['inside_focus_slope']))
                else:
                    # Do a final move to (approximately) the target HFD
                    self._current_focus += int((self._config['target_hfd'] - current_hfd) /
                                               self._config['inside_focus_slope'])
                    self.state = AutoFocusState.MeasureTargetHFD

                if not focus_set(self._log_name, self.camera_id, self._current_focus):
                    self._set_error()
                    return

            elif self.state == AutoFocusState.MeasureTargetHFD:
                # Jump to target focus using calibrated parameters
                self._current_focus += int((self._config['crossing_hfd'] - current_hfd) /
                                           self._config['inside_focus_slope'])
                self.state = AutoFocusState.MeasureFinalHFD

                if not focus_set(self._log_name, self.camera_id, self._current_focus):
                    self._set_error()
                    return
            elif self.state == AutoFocusState.MeasureFinalHFD:
                runtime = (Time.now() - self._start_time).to_value(u.s)
                log.info(self._log_name,
                         f'AutoFocus: camera {self.camera_id} achieved HFD of {current_hfd:.1f}" ' +
                         f'in {runtime:.0f} seconds')

                self.state = AutoFocusState.Complete

            if self._best_hfd is None:
                self._best_hfd = current_hfd
            else:
                self._best_hfd = np.fmin(self._best_hfd, current_hfd)

        self._take_image()

    def abort(self):
        """Aborts any active exposures and sets the state to complete"""
        # Assume that focus images are always short and we can just wait for it to finish.
        # The recieved handler will handle restoring the original focus position
        if self.state < AutoFocusState.Complete:
            self.state = AutoFocusState.Aborting


CONFIG = {
    # The slope (in hfd / step) on the inside edge of the v-curve
    'inside_focus_slope': -0.0030321,

    # The HFD value where the two v-curve edges cross
    # This is a more convenient way of representing the position intercept difference
    'crossing_hfd': 3.2,

    # Threshold HFD that is used to filter junk
    # Real stars should never be smaller than this
    'minimum_hfd': 2.5,

    # Number of objects that are required to consider MEDHFD valid
    'minimum_object_count': 50,

    # Aim to reach this HFD on the inside edge of the v-curve
    # before offsetting to the final focus
    'target_hfd': 6,

    # Number of measurements to take when moving in to find the target HFD
    'coarse_measure_repeats': 3,

    # Number of measurements to take when sampling the target and final HFDs
    'fine_measure_repeats': 7,

    # Number of focuser steps to move when searching for the target HFD
    'focus_step_size': 1000,

    # Number of seconds to add to the exposure time to account for readout + object detection
    # Consider the frame lost if this is exceeded
    'max_processing_time': 20
}
