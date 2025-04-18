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

import threading
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time
import astropy.units as u
from rockit.common import log, validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cameras, cam_take_images
from .coordinate_helpers import zenith_radec
from .focus_helpers import focus_get, focus_set
from .mount_helpers import mount_slew_radec, mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema

LOOP_INTERVAL = 5


class Progress:
    Waiting, Slewing, Blue, Red = range(4)


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
        "blue": { # Optional: cameras that aren't listed won't be focused
            "exposure": 1
            # Also supports optional bin, window, temperature, gainindex, readoutindex (advanced options)
        }
    }
    """
    def __init__(self, **args):
        super().__init__('Auto Focus', **args)
        self._wait_condition = threading.Condition()
        self._progress = Progress.Waiting
        self._current_state = AutoFocusState.MeasureInitial

        if 'start' in self.config:
            self._start_date = Time(self.config['start'])
        else:
            self._start_date = None

        if 'expires' in self.config:
            self._expires_date = Time(self.config['expires'])
        else:
            self._expires_date = None

        self._focus_measurement = None

        # Blue (i.e. telescope) focus impacts red, so must be done first
        self._camera_ids = []
        for camera_id in ['blue', 'red']:
            if camera_id in self.config:
                self._camera_ids.append(camera_id)

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []

        if self._progress <= Progress.Waiting and self._start_date is not None and Time.now() < self._start_date:
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

        if self._progress < Progress.Blue and 'blue' in self._camera_ids:
            tasks.append('Focus Blue camera')
        elif self._progress == Progress.Blue:
            tasks.append(f'Focus Blue camera ({AutoFocusState.Labels[self._current_state]})')

        if self._progress < Progress.Red and 'red' in self._camera_ids:
            tasks.append('Focus Red camera')
        elif self._progress == Progress.Red:
            tasks.append(f'Focus Red camera ({AutoFocusState.Labels[self._current_state]})')

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        if self._start_date is not None and Time.now() < self._start_date:
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        while not self.aborted and not self.dome_is_open:
            if self._expires_date is not None and Time.now() > self._expires_date:
                break

            with self._wait_condition:
                self._wait_condition.wait(LOOP_INTERVAL)

        if self.aborted or self._expires_date is not None and Time.now() > self._expires_date:
            self.status = TelescopeActionStatus.Complete
            return

        pipeline_config = {
            'hfd': True,
            'type': 'JUNK'
        }

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        # Fall back to zenith if coords not specified
        zenith_ra, zenith_dec = zenith_radec(self.site_location)
        ra = self.config.get('ra', zenith_ra)
        dec = self.config.get('dec', zenith_dec)

        self._progress = Progress.Slewing
        if not mount_slew_radec(self.log_name, ra, dec, True, open_covers=True):
            self.status = TelescopeActionStatus.Error
            return

        for camera_id in self._camera_ids:
            self._current_state = AutoFocusState.MeasureInitial
            if camera_id == 'blue':
                self._progress = Progress.Blue
            elif camera_id == 'red':
                self._progress = Progress.Red

            camera_config = CONFIG[camera_id]
            start_time = Time.now()
            success = False
            initial_focus = current_focus = focus_get(self.log_name, camera_id)
            if current_focus is None:
                continue

            try:
                log.info(self.log_name, f'AutoFocus: Focusing {camera_id}')
                initial_hfd = min_hfd = self.measure_current_hfd(camera_id, camera_config['coarse_measure_repeats'])
                if initial_hfd is None:
                    continue

                log.info(self.log_name, f'AutoFocus: HFD at {current_focus} steps is ' +
                         f'{initial_hfd:.1f}" ({camera_config["coarse_measure_repeats"]} samples)')

                self._current_state = AutoFocusState.FindPositionOnVCurve

                # Step inwards until we are well defocused on the inside edge of the v curve
                failed = False
                log.info(self.log_name, 'AutoFocus: Searching for position on v-curve')
                while True:
                    current_focus -= camera_config['focus_step_size']
                    if not focus_set(self.log_name, camera_id, current_focus):
                        failed = True
                        break

                    current_hfd = self.measure_current_hfd(camera_id, camera_config['coarse_measure_repeats'])
                    if current_hfd is None:
                        failed = True
                        break

                    log.info(self.log_name, f'AutoFocus: HFD at {current_focus} steps is ' +
                             f'{current_hfd:.1f}" ({camera_config["coarse_measure_repeats"]} samples)')

                    min_hfd = min(min_hfd, current_hfd)
                    if current_hfd > camera_config['target_hfd'] and current_hfd > min_hfd:
                        log.info(self.log_name, 'AutoFocus: Found position on v-curve')
                        break

                if failed:
                    continue

                # We may have stepped to far inwards in the previous step
                # Step outwards if needed until the current HFD is closer to the target
                self._current_state = AutoFocusState.FindTargetHFD
                failed = False
                while current_hfd > 2 * camera_config['target_hfd']:
                    log.info(self.log_name, f'AutoFocus: Stepping towards HFD {camera_config["target_hfd"]}')

                    current_focus -= int(current_hfd / (2 * camera_config['inside_focus_slope']))
                    if not focus_set(self.log_name, camera_id, current_focus):
                        failed = True
                        break

                    current_hfd = self.measure_current_hfd(camera_id, camera_config['coarse_measure_repeats'])
                    if current_hfd is None:
                        failed = True
                        break

                    log.info(self.log_name, f'AutoFocus: HFD at {current_focus} steps is ' +
                             f'{current_hfd:.1f}" ({camera_config["coarse_measure_repeats"]} samples)')

                if failed:
                    continue

                # Do a final move to (approximately) the target HFD
                current_focus += int((camera_config['target_hfd'] - current_hfd) / camera_config['inside_focus_slope'])
                if not focus_set(self.log_name, camera_id, current_focus):
                    continue

                # Take more frames to get an improved HFD estimate at the current position
                self._current_state = AutoFocusState.MeasureTargetHFD
                current_hfd = self.measure_current_hfd(camera_id, camera_config['fine_measure_repeats'])
                if current_hfd is None:
                    continue

                log.info(self.log_name, f'AutoFocus: HFD at {current_focus} steps is ' +
                         f'{current_hfd:.1f}" ({camera_config["fine_measure_repeats"]} samples)')

                # Jump to target focus using calibrated parameters
                current_focus += int(
                    (camera_config['crossing_hfd'] - current_hfd) / camera_config['inside_focus_slope'])

                if not focus_set(self.log_name, camera_id, current_focus):
                    continue

                self._current_state = AutoFocusState.MeasureFinalHFD
                current_hfd = self.measure_current_hfd(camera_id, camera_config['fine_measure_repeats'])
                if current_hfd is None:
                    continue

                runtime = (Time.now() - start_time).to_value(u.s)

                log.info(self.log_name, f'AutoFocus: Achieved HFD of {current_hfd:.1f}" in {runtime:.0f} seconds')
                success = current_hfd <= initial_hfd
            finally:
                if not success and initial_focus is not None:
                    log.info(self.log_name, 'Restoring initial focus position')
                    focus_set(self.log_name, camera_id, initial_focus)

        mount_stop(self.log_name)
        self.status = TelescopeActionStatus.Complete

    def measure_current_hfd(self, camera_id, exposures=1):
        """ Takes a set of exposures and returns the smallest MEDHFD value
            Returns None on error
        """
        camera_config = CONFIG[camera_id]
        requested = exposures
        failed = 0

        cam_config = self.config[camera_id].copy()
        cam_config['shutter'] = True

        # Handle exposures individually
        # This adds a few seconds of overhead when we want to take
        # multiple samples, but this is the simpler/safer option
        samples = []
        while True:
            if len(samples) == requested:
                print('hfd values:', samples)
                return np.min(samples)

            if failed > 5:
                log.error(self.log_name, 'AutoFocus: Aborting because 5 HFD samples failed')
                return None

            if not cam_take_images(self.log_name, camera_id, 1, cam_config, quiet=True):
                return None

            expected_complete = Time.now() + (cam_config['exposure'] + camera_config['max_processing_time']) * u.s

            while True:
                if not self.dome_is_open:
                    log.error(self.log_name, 'AutoFocus: Aborting because dome is not open')
                    return None

                if self.aborted:
                    log.error(self.log_name, 'AutoFocus: Aborted by user')
                    return None

                if self._focus_measurement:
                    hfd, count = self._focus_measurement
                    self._focus_measurement = None
                    if count > camera_config['minimum_object_count'] and hfd > camera_config['minimum_hfd']:
                        samples.append(hfd)
                    else:
                        log.warning(self.log_name, f'AutoFocus: Discarding frame with {count} samples ({hfd} HFD)')
                        failed += 1
                    break

                if Time.now() > expected_complete:
                    log.warning(self.log_name, 'AutoFocus: Exposure timed out - retrying')
                    failed += 1
                    break

                with self._wait_condition:
                    self._wait_condition.wait(LOOP_INTERVAL)

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
        with self._wait_condition:
            if 'MEDHFD' in headers and 'HFDCNT' in headers:
                self._focus_measurement = (headers['MEDHFD'], headers['HFDCNT'])
            else:
                print('Headers are missing MEDHFD or HFDCNT')
                print(headers)
                self._focus_measurement = (0, 0)
            self._wait_condition.notify_all()

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
                'dec': ['ra'],
                'start': ['expires']
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


CONFIG = {
    'blue': {
        # The slope (in hfd / step) on the inside edge of the v-curve
        'inside_focus_slope': -0.020337,

        # The HFD value where the two v-curve edges cross
        # This is a more convenient way of representing the position intercept difference
        'crossing_hfd': 1.1,

        # Threshold HFD that is used to filter junk
        # Real stars should never be smaller than this
        'minimum_hfd': 1.5,

        # Number of objects that are required to consider MEDHFD valid
        'minimum_object_count': 10,

        # Aim to reach this HFD on the inside edge of the v-curve
        # before offsetting to the final focus
        'target_hfd': 4.5,

        # Number of measurements to take when moving in to find the target HFD
        'coarse_measure_repeats': 3,

        # Number of measurements to take when sampling the target and final HFDs
        'fine_measure_repeats': 7,

        # Number of focuser steps to move when searching for the target HFD
        'focus_step_size': 100,

        # Number of seconds to add to the exposure time to account for readout + object detection
        # Consider the frame lost if this is exceeded
        'max_processing_time': 20
    },
    'red': {
        # The slope (in hfd / step) on the inside edge of the v-curve
        'inside_focus_slope': -0.0006526110711109228,

        # The HFD value where the two v-curve edges cross
        # This is a more convenient way of representing the position intercept difference
        'crossing_hfd': 0.943,

        # Threshold HFD that is used to filter junk
        # Real stars should never be smaller than this
        'minimum_hfd': 1.5,

        # Number of objects that are required to consider MEDHFD valid
        'minimum_object_count': 10,

        # Aim to reach this HFD on the inside edge of the v-curve
        # before offsetting to the final focus
        'target_hfd': 4.5,

        # Number of measurements to take when moving in to find the target HFD
        'coarse_measure_repeats': 3,

        # Number of measurements to take when sampling the target and final HFDs
        'fine_measure_repeats': 7,

        # Number of focuser steps to move when searching for the target HFD
        'focus_step_size': 500,

        # Number of seconds to add to the exposure time to account for readout + object detection
        # Consider the frame lost if this is exceeded
        'max_processing_time': 20
    }
}
