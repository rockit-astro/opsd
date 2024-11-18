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

"""Telescope action to find focus using the v-curve technique"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import queue
import threading
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time
import astropy.units as u
from rockit.common import log, validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cameras, cam_configure, cam_take_images
from .coordinate_helpers import zenith_radec
from .focus_helpers import focus_set, focus_get
from .mount_helpers import mount_slew_radec, mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema


class Progress:
    Waiting, Slewing, Focusing = range(3)


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
        "cmos": { # Optional: cameras that aren't listed won't be focused
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, window, gain, offset, stream (advanced options)
        },
        "swir": { # Optional: cameras that aren't listed won't be focused
            "exposure": 1,
            # Also supports optional temperature (advanced options)
        }
    }
    """
    def __init__(self, **args):
        super().__init__('Auto Focus', **args)
        self._wait_condition = threading.Condition()
        self._progress = Progress.Waiting

        if 'start' in self.config:
            self._start_date = Time(self.config['start'])
        else:
            self._start_date = None

        if 'expires' in self.config:
            self._expires_date = Time(self.config['expires'])
        else:
            self._expires_date = None

        self._cameras = {}
        for camera_id in cameras:
            camera_config = {}
            camera_config.update(CONFIG)
            camera_config.update(CAMERA_CONFIG.get(camera_id, {}))
            self._cameras[camera_id] = CameraWrapper(camera_id, camera_config, self.config.get(camera_id, None),
                                                     self.log_name)

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []

        if self._progress <= Progress.Waiting:
            if self._start_date:
                tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')
        elif not self.dome_is_open:
            label = 'Wait for dome'
            if self._expires_date:
                label += f' (expires {self._expires_date.strftime("%H:%M:%S")})'
            tasks.append(label)

        if self._progress <= Progress.Slewing:
            ra = self.config.get('ra', None)
            dec = self.config.get('dec', None)
            if ra and dec:
                coord = SkyCoord(ra=ra, dec=dec, unit=u.deg)
                tasks.append(f'Slew to {coord.to_string("hmsdms", sep=":", precision=0)}')
            else:
                tasks.append('Slew to zenith')

        if self._progress < Progress.Focusing:
            camera_ids = [c.camera_id for c in self._cameras.values() if c.state != AutoFocusState.Complete]
            tasks.append(f'Run AutoFocus ({", ".join(camera_ids)})')
        elif self._progress == Progress.Focusing:
            tasks.append('Run AutoFocus:')
            camera_state = []
            for camera_id, camera in self._cameras.items():
                camera_state.append(f'{camera_id}: {AutoFocusState.Labels[camera.state]}')
            tasks.append(camera_state)

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        if self._start_date is not None and Time.now() < self._start_date:
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        while not self.aborted and not self.dome_is_open:
            if self._expires_date is not None and Time.now() > self._expires_date:
                break

            with self._wait_condition:
                self._wait_condition.wait(10)

        if self.aborted or self._expires_date is not None and Time.now() > self._expires_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Fall back to zenith if coords not specified
        zenith_ra, zenith_dec = zenith_radec(self.site_location)
        ra = self.config.get('ra', zenith_ra)
        dec = self.config.get('dec', zenith_dec)

        self._progress = Progress.Slewing
        if not mount_slew_radec(self.log_name, ra, dec, True):
            self.status = TelescopeActionStatus.Error
            return

        self._progress = Progress.Focusing

        pipeline_config = {
            'hfd': True,
            'type': 'JUNK'
        }

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        # This starts the autofocus logic, which is run on camera-specific threads
        for camera in self._cameras.values():
            camera.start()

        # Wait until complete
        while True:
            with self._wait_condition:
                self._wait_condition.wait(5)

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
            },
            'dependencies': {
                'ra': ['dec'],
                'dec': ['ra']
            }
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema(camera_id)

        return validation.validation_errors(config_json, schema)


class AutoFocusState:
    """Possible states of the AutoFocus routine"""
    MeasureInitial, FindPositionOnVCurve, FindTargetHFD, MeasureTargetHFD, \
        MeasureFinalHFD, Aborting, Complete, Failed, Error = range(9)

    Labels = {
        0: 'Measuring initial HFD',
        1: 'Finding position on V curve',
        2: 'Moving to target HFD',
        3: 'Measuring HFD',
        4: 'Measuring final HFD',
        5: 'Aborting',
        6: 'Complete',
        7: 'Failed',
        8: 'Error'
    }


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
        self._received_queue = queue.Queue()

    def _run(self):
        """Thread running the main state machine"""
        start_time = Time.now()
        measurements = []
        failed_measurements = 0
        best_hfd = None
        exposure_timeout = (self._camera_config['exposure'] + self._config['max_processing_time']) * u.s

        # Assign to shorter variable names to improve readability
        camera_id = self.camera_id
        log_name = self._log_name
        inside_focus_slope = self._config['inside_focus_slope']
        crossing_hfd = self._config['crossing_hfd']
        target_hfd = self._config['target_hfd']
        minimum_hfd = self._config['minimum_hfd']
        minimum_object_count = self._config['minimum_object_count']
        focus_step_size = self._config['focus_step_size']
        search_hfd_increase = self._config['search_hfd_increase']
        coarse_measure_repeats = self._config['coarse_measure_repeats']
        fine_measure_repeats = self._config['fine_measure_repeats']
        fine_measure_states = [AutoFocusState.MeasureTargetHFD, AutoFocusState.MeasureFinalHFD]

        # Record the initial focus so we can return on error
        initial_focus = current_focus = focus_get(log_name, camera_id)
        if initial_focus is None:
            self.state = AutoFocusState.Error
            return

        # Set the camera config once at the start to avoid duplicate changes
        cam_config = self._camera_config.copy()
        if camera_id == 'cmos':
            cam_config['stream'] = False

        if not cam_configure(log_name, camera_id, cam_config):
            self.state = AutoFocusState.Error
            return

        expected_complete = Time.now() + exposure_timeout
        if not cam_take_images(log_name, camera_id, quiet=True):
            self.state = AutoFocusState.Error
            return

        class Failed(Exception):
            pass

        class Error(Exception):
            pass

        try:
            while True:
                try:
                    hfd, count = self._received_queue.get(timeout=5)
                    if hfd is None or count is None:
                        log.warning(log_name, f'AutoFocus: camera {camera_id} discarding frame without HFD headers')
                        failed_measurements += 1
                    elif count < minimum_object_count or hfd < minimum_hfd:
                        log.warning(log_name, f'AutoFocus: camera {camera_id} discarding frame with {count} samples ({hfd} HFD)')
                        failed_measurements += 1
                    else:
                        measurements.append(hfd)
                except queue.Empty:
                    if expected_complete and Time.now() > expected_complete:
                        log.error(log_name, f'AutoFocus: camera {camera_id} exposure timed out')
                        failed_measurements += 1
                    else:
                        continue

                if self.state >= AutoFocusState.Complete:
                    break

                if self.state == AutoFocusState.Aborting:
                    raise Failed

                if failed_measurements == 5:
                    log.error(log_name, f'AutoFocus: camera {camera_id} aborting because 5 HFD samples failed')
                    raise Failed

                requested = fine_measure_repeats if self.state in fine_measure_states else coarse_measure_repeats

                if len(measurements) == requested:
                    print(camera_id, ' hfd values:', measurements)
                    current_hfd = float(np.min(measurements))
                    log.info(log_name, f'AutoFocus: camera {camera_id} HFD at {current_focus} steps is {current_hfd:.1f}" ({requested} samples)')

                    measurements.clear()
                    failed_measurements = 0

                    if self.state == AutoFocusState.MeasureInitial:
                        self.state = AutoFocusState.FindPositionOnVCurve

                    if self.state == AutoFocusState.FindPositionOnVCurve:
                        # Step inwards until we are well defocused on the inside edge of the v curve
                        if best_hfd is not None and current_hfd > best_hfd + search_hfd_increase and current_hfd > target_hfd:
                            log.info(log_name, f'AutoFocus: camera {camera_id} found position on v-curve')
                            self.state = AutoFocusState.FindTargetHFD
                        else:
                            current_focus -= focus_step_size
                            if not focus_set(log_name, camera_id, current_focus):
                                raise Error

                    # Note: not an elif to allow the FindPositionOnVCurve case above to enter this branch too
                    if self.state == AutoFocusState.FindTargetHFD:
                        # We may have stepped to far inwards in the previous step
                        # Step outwards if needed until the current HFD is closer to the target
                        if current_hfd > 2 * target_hfd:
                            log.info(log_name, f'AutoFocus: camera {camera_id} stepping towards HFD {target_hfd}')

                            current_focus -= int(current_hfd / (2 * inside_focus_slope))
                        else:
                            # Do a final move to (approximately) the target HFD
                            current_focus += int((target_hfd - current_hfd) / inside_focus_slope)
                            self.state = AutoFocusState.MeasureTargetHFD

                        if not focus_set(log_name, camera_id, current_focus):
                            raise Error

                    elif self.state == AutoFocusState.MeasureTargetHFD:
                        # Jump to target focus using calibrated parameters
                        current_focus += int((crossing_hfd - current_hfd) / inside_focus_slope)
                        self.state = AutoFocusState.MeasureFinalHFD

                        if not focus_set(log_name, camera_id, current_focus):
                            raise Error

                    elif self.state == AutoFocusState.MeasureFinalHFD:
                        runtime = (Time.now() - start_time).to_value(u.s)
                        log.info(log_name, f'AutoFocus: camera {camera_id} achieved HFD of {current_hfd:.1f}" in {runtime:.0f} seconds')
                        self.state = AutoFocusState.Complete
                        return

                    if best_hfd is None:
                        best_hfd = current_hfd
                    else:
                        best_hfd = np.fmin(best_hfd, current_hfd)

                expected_complete = Time.now() + exposure_timeout
                if not cam_take_images(log_name, camera_id, quiet=True):
                    raise Error
        except Failed:
            if not focus_set(log_name, camera_id, initial_focus):
                log.error(log_name, f'AutoFocus: camera {camera_id} failed to restore initial focus')
            self.state = AutoFocusState.Failed
        except Exception:
            if not focus_set(log_name, camera_id, initial_focus):
                log.error(log_name, f'AutoFocus: camera {camera_id} failed to restore initial focus')
            self.state = AutoFocusState.Error

    def start(self):
        """Starts the autofocus sequence for this camera"""
        if self.state == AutoFocusState.Complete:
            return

        threading.Thread(target=self._run).start()

    def abort(self):
        """Aborts any active exposures and sets the state to complete"""
        # Assume that focus images are always short so we can just wait for the state machine to clean up
        if self.state < AutoFocusState.Complete:
            self.state = AutoFocusState.Aborting

    def received_frame(self, headers):
        """Callback to process an acquired frame. headers is a dictionary of header keys"""
        if self.state >= AutoFocusState.Complete:
            return

        self._received_queue.put((headers.get('MEDHFD', None), headers.get('HFDCNT', None)))


CONFIG = {
    # Threshold HFD that is used to filter junk
    # Real stars should never be smaller than this
    'minimum_hfd': 2.5,

    # Aim to reach this HFD on the inside edge of the v-curve
    # before offsetting to the final focus
    'target_hfd': 6,

    # Number of measurements to take when moving in to find the target HFD
    'coarse_measure_repeats': 3,

    # Number of measurements to take when sampling the target and final HFDs
    'fine_measure_repeats': 7,

    # Number of focuser steps to move when searching for the target HFD
    'focus_step_size': 50,

    # Number of seconds to add to the exposure time to account for readout + object detection
    # Consider the frame lost if this is exceeded
    'max_processing_time': 60,

    # Keep moving focus until the HFD increases by this many arcseconds above the best measured value
    # when searching for the initial position on the V curve
    'search_hfd_increase': 3
}

CAMERA_CONFIG = {
    'cmos': {
        # The slope (in hfd / step) on the inside edge of the v-curve
        'inside_focus_slope': -0.06504,

        # The HFD value where the two v-curve edges cross
        # This is a more convenient way of representing the position intercept difference
        'crossing_hfd': 0,

        # Number of objects that are required to consider MEDHFD valid
        'minimum_object_count': 50,
    },
    'swir': {
        # The slope (in hfd / step) on the inside edge of the v-curve
        'inside_focus_slope': -0.0415,

        # The HFD value where the two v-curve edges cross
        # This is a more convenient way of representing the position intercept difference
        'crossing_hfd': 2.7,

        # Number of objects that are required to consider MEDHFD valid
        'minimum_object_count': 10,
    },
}
