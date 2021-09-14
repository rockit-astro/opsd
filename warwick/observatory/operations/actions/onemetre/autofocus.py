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
from warwick.observatory.camera.andor import configure_validation_schema as camera_schema
from .telescope_helpers import tel_slew_radec, tel_status, tel_stop, tel_set_focus
from .camera_helpers import cam_take_images, cam_stop
from .pipeline_helpers import configure_pipeline

SLEW_TIMEOUT = 120
FOCUS_TIMEOUT = 300

# The slope (in hfd / step) on the inside edge of the v-curve
INSIDE_FOCUS_SLOPE = -0.020337

# The HFD value where the two v-curve edges cross
# This is a more convenient way of representing the position intercept difference
CROSSING_HFD = 1.1

# Threshold HFD that is used to filter junk
# Real stars should never be smaller than this
MINIMUM_HFD = 1.5

# Number of objects that are required to consider MEDHFD valid
MINIMUM_OBJECT_COUNT = 15

# Aim to reach this HFD on the inside edge of the v-curve
# before offsetting to the final focus
TARGET_HFD = 5

# Number of measurements to take when moving in to find the target HFD
COARSE_MEASURE_REPEATS = 3

# Number of measurements to take when sampling the target and final HFDs
FINE_MEASURE_REPEATS = 7

# Number of focuser steps to move when searching for the target HFD
FOCUS_STEP_SIZE = 250

# Number of seconds to add to the exposure time to account for readout + object detection
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = 10

# Note: pipeline and camera schemas are inserted in the validate_config method
CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['ra', 'dec'],
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
        }
    }
}


class AutoFocus(TelescopeAction):
    """Telescope action to find the optimium focus using the v-curve technique"""
    def __init__(self, log_name, config):
        super().__init__('Auto Focus', log_name, config)
        self._wait_condition = threading.Condition()
        self._camera_id = 'blue'
        self._focus_measurement = None

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {}
        schema.update(CONFIG_SCHEMA)

        # TODO: Support action config for blue or red (telescope or instrument) focus
        schema['properties']['blue'] = camera_schema('blue')
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

        if not tel_slew_radec(self.log_name, self.config['ra'], self.config['dec'], True, SLEW_TIMEOUT):
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
        first_hfd = min_hfd = self.measure_current_hfd(COARSE_MEASURE_REPEATS)
        if first_hfd < 0:
            self.__set_failed_status()
            return

        current_focus = tel_status(self.log_name).get('telescope_focus_um', None)
        if current_focus is None:
            self.__set_failed_status()
            return

        log.info(self.log_name, 'AutoFocus: HFD at {} steps is {:.1f}" ({} samples)'.format(
            current_focus, first_hfd, COARSE_MEASURE_REPEATS))

        self.set_task('Searching v-curve position')

        # Step inwards until we are well defocused on the inside edge of the v curve
        while True:
            log.info(self.log_name, 'AutoFocus: Searching for position on v-curve')
            print('AutoFocus: Searching for position on v-curve (stepping out until hfd > target)')
            current_focus -= FOCUS_STEP_SIZE
            if not tel_set_focus(self.log_name, current_focus, FOCUS_TIMEOUT):
                self.__set_failed_status()
                return

            current_hfd = self.measure_current_hfd(COARSE_MEASURE_REPEATS)
            if current_hfd < 0:
                self.__set_failed_status()
                return

            log.info(self.log_name, 'AutoFocus: HFD at {} steps is {:.1f}" ({} samples)'.format(
                current_focus, current_hfd, COARSE_MEASURE_REPEATS))

            min_hfd = min(min_hfd, current_hfd)
            if current_hfd > TARGET_HFD and current_hfd > min_hfd:
                log.info(self.log_name, 'AutoFocus: Found position on v-curve')
                print('AutoFocus: on inside slope')
                break

        # We may have stepped to far inwards in the previous step
        # Step outwards if needed until the current HFD is closer to the target
        self.set_task('Searching for HFD {}'.format(TARGET_HFD))
        while current_hfd > 2 * TARGET_HFD:
            log.info(self.log_name, 'AutoFocus: Stepping towards HFD {}'.format(TARGET_HFD))
            print('AutoFocus: Stepping towards HFD {}'.format(TARGET_HFD))

            current_focus -= int(current_hfd / (2 * INSIDE_FOCUS_SLOPE))
            if not tel_set_focus(self.log_name, current_focus, FOCUS_TIMEOUT):
                self.__set_failed_status()
                return

            current_hfd = self.measure_current_hfd(COARSE_MEASURE_REPEATS)
            if current_hfd < 0:
                self.__set_failed_status()
                return

            log.info(self.log_name, 'AutoFocus: HFD at {} steps is {:.1f}" ({} samples)'.format(
                current_focus, current_hfd, COARSE_MEASURE_REPEATS))

        # Do a final move to (approximately) the target HFD
        current_focus += int((TARGET_HFD - current_hfd) / INSIDE_FOCUS_SLOPE)
        if not tel_set_focus(self.log_name, current_focus, FOCUS_TIMEOUT):
            self.__set_failed_status()
            return

        # Take more frames to get an improved HFD estimate at the current position
        self.set_task('Sampling HFD for final move')
        current_hfd = self.measure_current_hfd(FINE_MEASURE_REPEATS)
        if current_hfd < 0:
            self.__set_failed_status()
            return

        log.info(self.log_name, 'AutoFocus: HFD at {} steps is {:.1f}" ({} samples)'.format(
            current_focus, current_hfd, FINE_MEASURE_REPEATS))

        # Jump to target focus using calibrated parameters
        current_focus += int((CROSSING_HFD - current_hfd) / INSIDE_FOCUS_SLOPE)

        self.set_task('Moving to focus')
        if not tel_set_focus(self.log_name, current_focus, FOCUS_TIMEOUT):
            self.__set_failed_status()
            return

        self.set_task('Sampling final HFD')
        current_hfd = self.measure_current_hfd(FINE_MEASURE_REPEATS)
        runtime = (datetime.datetime.utcnow() - start_time).total_seconds()

        print('AutoFocus: Achieved HFD of {:.1f}" in {:.0f} seconds'.format(current_hfd, runtime))
        log.info(self.log_name, 'AutoFocus: Achieved HFD of {:.1f}" in {:.0f} seconds'.format(
            current_hfd, runtime))

        self.status = TelescopeActionStatus.Complete

    def measure_current_hfd(self, exposures=1):
        """ Takes a set of exposures and returns the smallest MEDHFD value
            Returns -1 on error
        """
        print('AutoFocus: Sampling HFD')
        log.info(self.log_name, 'AutoFocus: Sampling HFD')

        requested = exposures
        failed = 0

        cam_config = {}
        cam_config.update(self.config.get(self._camera_id, {}))
        cam_config.update({
            'shutter': True
        })

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
                print(failed, 'AutoFocus: Aborting because 5 HFD samples failed')
                return -1

            if not cam_take_images(self.log_name, self._camera_id, 1, cam_config, quiet=True):
                return -1

            delay = self.config[self._camera_id]['exposure'] + MAX_PROCESSING_TIME
            expected_complete = datetime.datetime.utcnow() + datetime.timedelta(seconds=delay)

            while True:
                if not self.dome_is_open:
                    log.error(self.log_name, 'AutoFocus: Aborting because dome is not open')
                    print(failed, 'AutoFocus: Aborting because dome is not open')
                    return -1

                if self.aborted:
                    log.error(self.log_name, 'AutoFocus: Aborted by user')
                    print('AutoFocus: Aborted by user')
                    return -1

                measurement = self._focus_measurement
                if measurement:
                    self._focus_measurement = None
                    if measurement[1] > MINIMUM_OBJECT_COUNT and measurement[0] > MINIMUM_HFD:
                        samples.append(measurement[0])
                    else:
                        warning = 'AutoFocus: Discarding frame with {} samples ({} HFD)'.format(
                            measurement[1], measurement[0])
                        print(warning)
                        log.warning(self.log_name, warning)
                        failed += 1
                    break

                if datetime.datetime.utcnow() > expected_complete:
                    print('AutoFocus: Exposure timed out - retrying')
                    log.warning(self.log_name, 'AutoFocus: Exposure timed out - retrying')
                    failed += 1
                    break

                with self._wait_condition:
                    self._wait_condition.wait(10)

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        tel_stop(self.log_name)
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
        with self._wait_condition:
            if 'MEDHFD' in headers and 'HFDCNT' in headers:
                self._focus_measurement = (headers['MEDHFD'], headers['HFDCNT'])
            else:
                print('Headers are missing MEDHFD or HFDCNT')
                print(headers)
                self._focus_measurement = (0, 0)
            self._wait_condition.notify_all()
