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

"""Telescope action to find the optimium focus using the v-curve technique"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import datetime
import threading
import numpy as np

from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import log, validation
from warwick.observatory.pipeline import configure_standard_validation_schema as pipeline_schema
from warwick.observatory.camera.fli import configure_validation_schema as fli_camera_schema
from warwick.observatory.camera.qhy import configure_validation_schema as qhy_camera_schema
from .mount_helpers import mount_slew_radec, mount_stop
from .focus_helpers import focus_set, focus_get
from .camera_helpers import cam_take_images, cam_stop
from .pipeline_helpers import configure_pipeline

SLEW_TIMEOUT = 120
FOCUS_TIMEOUT = 300

CONFIG = {
    'fli1': {
        # The slope (in hfd / step) on the inside edge of the v-curve
        'inside_focus_slope': -0.0028806,

        # The HFD value where the two v-curve edges cross
        # This is a more convenient way of representing the position intercept difference
        'crossing_hfd': 2.9,

        # Threshold HFD that is used to filter junk
        # Real stars should never be smaller than this
        'minimum_hfd': 3.2,

        # Number of objects that are required to consider MEDHFD valid
        'minimum_object_count': 75,

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
    },

    'cam2': {
        # The slope (in hfd / step) on the inside edge of the v-curve
        'inside_focus_slope': -0.0030321,

        # The HFD value where the two v-curve edges cross
        # This is a more convenient way of representing the position intercept difference
        'crossing_hfd': 3.2,

        # Threshold HFD that is used to filter junk
        # Real stars should never be smaller than this
        'minimum_hfd': 3.2,

        # Number of objects that are required to consider MEDHFD valid
        'minimum_object_count': 75,

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
}


# Note: pipeline and camera schemas are inserted in the validate_config method
CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['ra', 'dec', 'camera'],
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
        'camera': {
            'type': 'string',
            'enum': ['fli1', 'cam2']
        }
    }
}


class AutoFocus(TelescopeAction):
    """Telescope action to find the optimium focus using the v-curve technique"""
    def __init__(self, log_name, config):
        super().__init__('Auto Focus', log_name, config)
        self._wait_condition = threading.Condition()

        # TODO: Support focusing both cameras in parallel
        self._camera_id = config['camera']
        self._config = CONFIG[self._camera_id]
        self._focuser_channel = 1 if self._camera_id == 'fli1' else 2
        self._focus_measurement = None

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {}
        schema.update(CONFIG_SCHEMA)

        # TODO: Support focusing both cameras in parallel
        schema['properties']['fli1'] = fli_camera_schema('fli1')
        schema['properties']['cam2'] = qhy_camera_schema('cam2')
        schema['properties']['pipeline'] = pipeline_schema()

        return validation.validation_errors(config_json, schema)

    def __set_failed_status(self):
        """Sets self.status to Complete if aborted otherwise Error"""
        if self.aborted:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def run_thread(self):
        """Thread that runs the hardware actions"""
        self.set_task('Slewing to field')

        if not mount_slew_radec(self.log_name, self.config['ra'], self.config['dec'], True, SLEW_TIMEOUT):
            self.__set_failed_status()
            return

        self.set_task('Preparing camera')
        start_time = datetime.datetime.utcnow()

        pipeline_config = {}
        pipeline_config.update(self.config.get('pipeline', {}))
        pipeline_config.update({
            'hfd': True,
            'type': 'JUNK',
            'object': 'AutoFocus',
        })

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.__set_failed_status()
            return

        self.set_task('Sampling initial HFD')
        first_hfd = min_hfd = self.measure_current_hfd(self._config['coarse_measure_repeats'])
        if first_hfd < 0:
            self.__set_failed_status()
            return

        current_focus = focus_get(self.log_name, self._focuser_channel)
        if current_focus is None:
            self.__set_failed_status()
            return

        log.info(self.log_name, 'AutoFocus: HFD at {} steps is {:.1f}" ({} samples)'.format(
            current_focus, first_hfd, self._config['coarse_measure_repeats']))

        self.set_task('Searching v-curve position')

        # Step inwards until we are well defocused on the inside edge of the v curve
        while True:
            log.info(self.log_name, 'AutoFocus: Searching for position on v-curve')
            current_focus -= self._config['focus_step_size']
            if not focus_set(self.log_name, self._focuser_channel, current_focus, FOCUS_TIMEOUT):
                self.__set_failed_status()
                return

            current_hfd = self.measure_current_hfd(self._config['coarse_measure_repeats'])
            if current_hfd < 0:
                self.__set_failed_status()
                return

            log.info(self.log_name, 'AutoFocus: HFD at {} steps is {:.1f}" ({} samples)'.format(
                current_focus, current_hfd, self._config['coarse_measure_repeats']))

            min_hfd = min(min_hfd, current_hfd)
            if current_hfd > self._config['target_hfd'] and current_hfd > min_hfd:
                log.info(self.log_name, 'AutoFocus: Found position on v-curve')
                break

        # We may have stepped to far inwards in the previous step
        # Step outwards if needed until the current HFD is closer to the target
        self.set_task('Searching for HFD {}'.format(self._config['target_hfd']))
        while current_hfd > 2 * self._config['target_hfd']:
            log.info(self.log_name, 'AutoFocus: Stepping towards HFD {}'.format(self._config['target_hfd']))

            current_focus -= int(current_hfd / (2 * self._config['inside_focus_slope']))
            if not focus_set(self.log_name, self._focuser_channel, current_focus, FOCUS_TIMEOUT):
                self.__set_failed_status()
                return

            current_hfd = self.measure_current_hfd(self._config['coarse_measure_repeats'])
            if current_hfd < 0:
                self.__set_failed_status()
                return

            log.info(self.log_name, 'AutoFocus: HFD at {} steps is {:.1f}" ({} samples)'.format(
                current_focus, current_hfd, self._config['coarse_measure_repeats']))

        # Do a final move to (approximately) the target HFD
        current_focus += int((self._config['target_hfd'] - current_hfd) / self._config['inside_focus_slope'])
        if not focus_set(self.log_name, self._focuser_channel, current_focus, FOCUS_TIMEOUT):
            self.__set_failed_status()
            return

        # Take more frames to get an improved HFD estimate at the current position
        self.set_task('Sampling HFD for final move')
        current_hfd = self.measure_current_hfd(self._config['fine_measure_repeats'])
        if current_hfd < 0:
            self.__set_failed_status()
            return

        log.info(self.log_name, 'AutoFocus: HFD at {} steps is {:.1f}" ({} samples)'.format(
            current_focus, current_hfd, self._config['fine_measure_repeats']))

        # Jump to target focus using calibrated parameters
        current_focus += int((self._config['crossing_hfd'] - current_hfd) / self._config['inside_focus_slope'])

        self.set_task('Moving to focus')
        if not focus_set(self.log_name, self._focuser_channel, current_focus, FOCUS_TIMEOUT):
            self.__set_failed_status()
            return

        self.set_task('Sampling final HFD')
        current_hfd = self.measure_current_hfd(self._config['fine_measure_repeats'])
        runtime = (datetime.datetime.utcnow() - start_time).total_seconds()

        log.info(self.log_name, 'AutoFocus: Achieved HFD of {:.1f}" in {:.0f} seconds'.format(
            current_hfd, runtime))

        self.status = TelescopeActionStatus.Complete

    def measure_current_hfd(self, exposures=1):
        """ Takes a set of exposures and returns the smallest MEDHFD value
            Returns -1 on error
        """
        log.info(self.log_name, 'AutoFocus: Sampling HFD')

        requested = exposures
        failed = 0

        cam_config = {}
        cam_config.update(self.config.get(self._camera_id, {}))
        if self._camera_id == 'fli1':
            cam_config['shutter'] = True

        # Handle exposures individually
        # This adds a few seconds of overhead when we want to take
        # multiple samples, but this is the simpler/safer option for nows
        samples = []
        while True:
            if len(samples) == requested:
                print('hfd values:', samples)
                return np.min(samples)

            if failed > 5:
                log.error(self.log_name, 'AutoFocus: Aborting because 5 HFD samples failed')
                return -1

            if not cam_take_images(self.log_name, self._camera_id, 1, cam_config, quiet=True):
                return -1

            delay = self.config[self._camera_id]['exposure'] + self._config['max_processing_time']
            expected_complete = datetime.datetime.utcnow() + datetime.timedelta(seconds=delay)

            while True:
                if not self.dome_is_open:
                    log.error(self.log_name, 'AutoFocus: Aborting because dome is not open')
                    return -1

                if self.aborted:
                    log.error(self.log_name, 'AutoFocus: Aborted by user')
                    return -1

                measurement = self._focus_measurement
                if measurement:
                    self._focus_measurement = None
                    if measurement[1] > self._config['minimum_object_count'] and measurement[0] > self._config['minimum_hfd']:
                        samples.append(measurement[0])
                    else:
                        warning = 'AutoFocus: Discarding frame with {} samples ({} HFD)'.format(
                            measurement[1], measurement[0])
                        log.warning(self.log_name, warning)
                        failed += 1
                    break

                if datetime.datetime.utcnow() > expected_complete:
                    log.warning(self.log_name, 'AutoFocus: Exposure timed out - retrying')
                    failed += 1
                    break

                with self._wait_condition:
                    self._wait_condition.wait(10)

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        mount_stop(self.log_name)
        cam_stop(self.log_name, self._camera_id)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        if headers.get('CAMID', '').lower() != self._camera_id:
            return

        with self._wait_condition:
            if 'MEDHFD' in headers and 'HFDCNT' in headers:
                self._focus_measurement = (headers['MEDHFD'], headers['HFDCNT'])
            else:
                print('Headers are missing MEDHFD or HFDCNT')
                print(headers)
                self._focus_measurement = (0, 0)
            self._wait_condition.notify_all()
