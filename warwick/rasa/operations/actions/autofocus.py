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
import threading
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
INSIDE_FOCUS_SLOPE = -9.05964912e-4

# The HFD value where the two v-curve edges cross
# This is a more convenient way of representing the position intercept difference
CROSSING_HFD = 3.4

# Aim to reach this HFD on the inside edge of the v-curve
# before offsetting to the final focus
TARGET_HFD = 6

# Gives a HFD step of ~3arcsec
FOCUS_STEP_SIZE = 2000

class AutoFocus(TelescopeAction):
    """Telescope action to find the optimium focus using the v-curve technique"""
    def __init__(self, config):
        super().__init__('Focus Sweep', config)
        self._wait_condition = threading.Condition()
        self._camera = daemons.rasa_camera
        self._focus_measurements = []

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

        pipeline_config = {}
        pipeline_config.update(self.config['pipeline'])
        pipeline_config.update({
            'fwhm': True,
            'type': 'SCIENCE',
            'object': 'Autofocus',
        })

        if not configure_pipeline(self.log_name, pipeline_config):
            self.status = TelescopeActionStatus.Error
            return

        first_hfd = self.measure_current_hfd()
        if first_hfd < 0:
            self.status = TelescopeActionStatus.Error
            return

        current_focus = get_focus(self.log_name, self.config['channel'])

        # Step inwards until we are well defocused on the inside edge of the v curve
        while True:
            current_focus -= FOCUS_STEP_SIZE
            if not set_focus(self.log_name, self.config['channel'], current_focus, FOCUS_TIMEOUT):
                self.__set_failed_status()
                return

            current_hfd = self.measure_current_hfd()
            if current_hfd < 0:
                self.__set_failed_status()
                return

            if current_hfd > TARGET_HFD and current_hfd > first_hfd:
                break

        # We may have stepped to far inwards in the previous step
        # Step outwards if needed until the current HFD is closer to the target
        while current_hfd > 2 * TARGET_HFD:
            current_focus -= current_hfd / (2 * INSIDE_FOCUS_SLOPE)
            if not set_focus(self.log_name, self.config['channel'], current_focus, FOCUS_TIMEOUT):
                self.__set_failed_status()
                return

            current_hfd = self.measure_current_hfd()
            if current_hfd < 0:
                self.__set_failed_status()
                return

        # Do a final move to (approximately) the target HFD
        current_focus += (TARGET_HFD - current_hfd) / INSIDE_FOCUS_SLOPE
        if not set_focus(self.log_name, self.config['channel'], current_focus, FOCUS_TIMEOUT):
            self.__set_failed_status()
            return

        # Take 5 frames to get an improved HFD estimate at the current position
        current_hfd = self.measure_current_hfd(5)
        if current_hfd < 0:
            self.__set_failed_status()
            return

        # Jump to target focus using calibrated parameters
        current_focus += (CROSSING_HFD - current_hfd) / INSIDE_FOCUS_SLOPE

        print('Final focus position is', current_focus)
        log.info(self.log_name, 'Final focus position is {}'.format(current_focus))
        if not set_focus(self.log_name, self.config['channel'], current_focus, FOCUS_TIMEOUT):
            self.__set_failed_status()
            return

        # Stop tracking for the next action
        if not tel_stop(self.log_name):
            self.status = TelescopeActionStatus.Error
            return

        self.status = TelescopeActionStatus.Complete

    def measure_current_hfd(self, exposures=1, count_threshold=200):
        """
            Returns -1 on error
        """
        requested = exposures
        failed = 0
        while True:
            if failed > 5:
                print(failed, 'exposured failed - giving up')

            self.set_task('Measuring HFD')
            if exposures > 0:
                if not take_images(self.log_name, self._camera, exposures, self.config['rasa']):
                    return -1

            expected_delay = exposures * (self.config['rasa']['exposure'] + 10)
            expected_complete = datetime.datetime.utcnow() \
                + datetime.timedelta(seconds=expected_delay)

            while True:
                if self.aborted or not self.dome_is_open:
                    return -1

                if len(self._focus_measurements) == requested:
                    # Reject and repeat bad measurements
                    filtered = [x for x in self._focus_measurements if x[1] > count_threshold]
                    if len(filtered) == requested:
                        print('hfd values:', [x[0] for x in filtered])
                        return np.median([x[0] for x in filtered])
                    else:
                        self._focus_measurements = filtered
                        exposures = requested - len(filtered)
                        failed += exposures

                # TODO: might need to consider a better check for the camera status
                if datetime.datetime.utcnow() > expected_complete:
                    print('Exposure timed out - retrying')
                    exposures = requested - len(self._focus_measurements)
                    failed += exposures

                with self._wait_condition:
                    self._wait_condition.wait(10)

    def received_frame(self, headers):
        """Received a frame from the pipeline"""
        with self._wait_condition:
            if 'MEDFWHM' in headers and 'FWHMCNT' in headers:
                print('got hfd', headers['MEDFWHM'], 'from', headers['FWHMCNT'], 'sources')
                self._focus_measurements.append((headers['MEDFWHM'], headers['FWHMCNT']))
            else:
                print('Headers are missing MEDFWHM or FWHMCNT')
                print(headers)
                self._focus_measurements.append((0, 0))
            self._wait_condition.notify_all()

    def abort(self):
        """Aborted by a weather alert or user action"""
        super().abort()

        tel_stop(self.log_name)
        stop_camera(self.log_name, self._camera)
        stop_focus(self.log_name)

        with self._wait_condition:
            self._wait_condition.notify_all()
