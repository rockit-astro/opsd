#!/usr/bin/env python3
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

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import datetime
import math
import sys
import threading
import traceback
import numpy as np
from warwick.observatory.common import daemons, log
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)
from warwick.rasa.camera import (
    configure_validation_schema as camera_schema)
from warwick.rasa.pipeline import (
    configure_standard_validation_schema as pipeline_schema)

from .camera_helpers import take_images, stop_camera
from .pipeline_helpers import configure_pipeline
from .telescope_helpers import get_focus, set_focus, stop_focus, tel_slew_radec, tel_stop

SLEW_TIMEOUT = 120
FOCUS_TIMEOUT = 300

# The slope (in hfd / step) on the inside edge of the v-curve
INSIDE_FOCUS_SLOPE = -1.09131672e-3

# The HFD value where the two v-curve edges cross
# This is a more convenient way of representing the position intercept difference
CROSSING_HFD = 2.2

# Threshold HFD that is used to filter junk
# Real stars should never be smaller than this
MINIMUM_HFD = 3.2

# Aim to reach this HFD on the inside edge of the v-curve
# before offsetting to the final focus
TARGET_HFD = 5

# Number of measurements to take when moving in to find the target HFD
COARSE_MEASURE_REPEATS = 3

# Number of measurements to take when sampling the target and final HFDs
FINE_MEASURE_REPEATS = 7

# Number of focuser steps to move when searching for the target HFD
FOCUS_STEP_SIZE = 2000

class AutoFocus(TelescopeAction):
    """Telescope action to find the optimium focus using the v-curve technique"""
    def __init__(self, config):
        super().__init__('Auto Focus', config)
        self._wait_condition = threading.Condition()
        self._camera = daemons.rasa_camera
        self._focus_measurement = None

    @classmethod
    def validation_schema(cls):
        # TODO: This will need to be generalized to support two focusers in the future
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['ra', 'dec', 'channel'],
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
                'channel': {
                    'type': 'integer',
                    'minimum': 2,
                    'maximum': 2
                },
                'rasa': camera_schema('rasa'),
                'pipeline': pipeline_schema()
            }
        }

    def __set_failed_status(self):
        """Sets self.status to Complete if aborted otherwise Error"""
        if self.aborted:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error
        return

    def run_thread(self):
        """Thread that runs the hardware actions"""
        self.set_task('Slewing to field')

        if not tel_slew_radec(self.log_name,
                              math.radians(self.config['ra']),
                              math.radians(self.config['dec']),
                              True, SLEW_TIMEOUT):
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

        if not configure_pipeline(self.log_name, pipeline_config):
            self.__set_failed_status()
            return

        first_hfd = min_hfd = self.measure_current_hfd(COARSE_MEASURE_REPEATS)
        if first_hfd < 0:
            self.__set_failed_status()
            return

        current_focus = get_focus(self.log_name, self.config['channel'])
        if current_focus is None:
            self.__set_failed_status()
            return

        # Step inwards until we are well defocused on the inside edge of the v curve
        while True:
            print('Stepping in until hfd > target')
            current_focus -= FOCUS_STEP_SIZE
            self.set_task('Finding target focus')
            if not set_focus(self.log_name, self.config['channel'],
                             current_focus, FOCUS_TIMEOUT):
                self.__set_failed_status()
                return

            current_hfd = self.measure_current_hfd(COARSE_MEASURE_REPEATS)
            if current_hfd < 0:
                self.__set_failed_status()
                return

            min_hfd = min(min_hfd, current_hfd)
            if current_hfd > TARGET_HFD and current_hfd > min_hfd:
                print('definitely inside focus')
                break

        # We may have stepped to far inwards in the previous step
        # Step outwards if needed until the current HFD is closer to the target
        while current_hfd > 2 * TARGET_HFD:
            print('too far from target HFD - stepping back out')
            current_focus -= int(current_hfd / (2 * INSIDE_FOCUS_SLOPE))
            self.set_task('Finding target focus')
            if not set_focus(self.log_name, self.config['channel'],
                             current_focus, FOCUS_TIMEOUT):
                self.__set_failed_status()
                return

            current_hfd = self.measure_current_hfd(COARSE_MEASURE_REPEATS)
            if current_hfd < 0:
                self.__set_failed_status()
                return

        # Do a final move to (approximately) the target HFD
        print('moving to where we think target HFD is')
        current_focus += int((TARGET_HFD - current_hfd) / INSIDE_FOCUS_SLOPE)
        if not set_focus(self.log_name, self.config['channel'], current_focus, FOCUS_TIMEOUT):
            self.__set_failed_status()
            return

        # Take 5 frames to get an improved HFD estimate at the current position
        current_hfd = self.measure_current_hfd(FINE_MEASURE_REPEATS)
        if current_hfd < 0:
            self.__set_failed_status()
            return

        # Jump to target focus using calibrated parameters
        current_focus += int((CROSSING_HFD - current_hfd) / INSIDE_FOCUS_SLOPE)

        print('Final focus position is', current_focus)
        self.set_task('Moving to final focus')
        log.info(self.log_name, 'Final focus position is {}'.format(current_focus))
        if not set_focus(self.log_name, self.config['channel'], current_focus, FOCUS_TIMEOUT):
            self.__set_failed_status()
            return

        current_hfd = self.measure_current_hfd(FINE_MEASURE_REPEATS)
        runtime = (datetime.datetime.utcnow() - start_time).total_seconds()

        print('Achieved HFD of {:.2f} in {:.0f} seconds'.format(current_hfd, runtime))
        log.info(self.log_name, 'Achieved HFD of {:.2f} in {:.0f} seconds'.format(
            current_hfd, runtime))
        self.status = TelescopeActionStatus.Complete


    def measure_current_hfd(self, exposures=1, count_threshold=200):
        """ Takes a set of exposures and returns the smallest MEDHFD value
            Returns -1 on error
        """
        requested = exposures
        failed = 0

        cam_config = {}
        cam_config.update(self.config.get('rasa', {}))
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
                print(failed, 'exposured failed - giving up')
                return -1

            self.set_task('Measuring HFD')
            if not take_images(self.log_name, self._camera, 1, cam_config, quiet=True):
                return -1

            expected_delay = exposures * (self.config['rasa']['exposure'] + 10)
            expected_complete = datetime.datetime.utcnow() \
                + datetime.timedelta(seconds=expected_delay)

            while True:
                if self.aborted or not self.dome_is_open:
                    print('aborted or dome closed')
                    return -1

                measurement = self._focus_measurement
                if measurement:
                    self._focus_measurement = None
                    if measurement[1] > count_threshold and measurement[0] > MINIMUM_HFD:
                        samples.append(measurement[0])
                    else:
                        failed += 1
                    break

                # TODO: might need to consider a better check for the camera status
                if datetime.datetime.utcnow() > expected_complete:
                    print('Exposure timed out - retrying')
                    failed += 1
                    break

                with self._wait_condition:
                    self._wait_condition.wait(10)

    def received_frame(self, headers):
        """Received a frame from the pipeline"""
        with self._wait_condition:
            if 'MEDHFD' in headers and 'HFDCNT' in headers:
                self._focus_measurement = (headers['MEDHFD'], headers['HFDCNT'])
            else:
                print('Headers are missing MEDHFD or HFDCNT')
                print(headers)
                self._focus_measurement = (0, 0)
            self._wait_condition.notify_all()

    def abort(self):
        """Aborted by a weather alert or user action"""
        super().abort()

        tel_stop(self.log_name)
        stop_camera(self.log_name, self._camera)
        stop_focus(self.log_name, self.config['channel'])

        with self._wait_condition:
            self._wait_condition.notify_all()
