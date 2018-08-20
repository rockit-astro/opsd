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

"""Telescope action to do a focus sweep on a defined field"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import datetime
import math
import threading
from warwick.observatory.common import daemons
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)
from warwick.rasa.camera import (
    configure_validation_schema as camera_schema)
from warwick.rasa.pipeline import (
    configure_standard_validation_schema as pipeline_schema)

from .camera_helpers import take_images, stop_camera
from .pipeline_helpers import configure_pipeline
from .telescope_helpers import set_focus, tel_slew_radec, tel_stop, stop_focus


SLEW_TIMEOUT = 120
FOCUS_TIMEOUT = 300

class FocusSweep(TelescopeAction):
    """Telescope action to do a focus sweep on a defined field"""
    def __init__(self, config):
        super().__init__('Focus Sweep', config)
        self._acquired_images = 0
        self._wait_condition = threading.Condition()
        self._focus_measurements = {}
        self._camera = daemons.rasa_camera

    @classmethod
    def validation_schema(cls):
        # TODO: This will need to be generalized to support two focusers in the future
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['ra', 'dec', 'channel', 'start', 'step', 'count'],
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
                'start': {
                    'type': 'integer',
                    'minimum': -20000,
                    'maximum': 20000
                },
                'step': {
                    'type': 'integer',
                },
                'count': {
                    'type': 'integer',
                    'minimum': 0
                },
                'rasa': camera_schema('rasa'),
                'pipeline': pipeline_schema()
            }
        }

    def run_thread(self):
        """Thread that runs the hardware actions"""
        self.set_task('Slewing to field')

        if not tel_slew_radec(self.log_name,
                              math.radians(self.config['ra']),
                              math.radians(self.config['dec']),
                              True, SLEW_TIMEOUT):

            if not self.aborted:
                self.status = TelescopeActionStatus.Error
            return

        if self.aborted:
            self.status = TelescopeActionStatus.Complete
            return

        self.set_task('Preparing camera')

        pipeline_config = {}
        pipeline_config.update(self.config['pipeline'])
        pipeline_config.update({
            'fwhm': True,
            'type': 'SCIENCE',
            'object': 'Focus run',
        })

        if not configure_pipeline(self.log_name, pipeline_config):
            self.status = TelescopeActionStatus.Error
            return

        # Move focuser to the start of the focus range
        current_focus = self.config['start']

        if not set_focus(self.log_name, self.config['channel'], current_focus, FOCUS_TIMEOUT):
            self.status = TelescopeActionStatus.Error
            return

        # Configure the camera then take the first exposure to start the process
        if not take_images(self.log_name, self._camera, 1, self.config['rasa']):
            self.status = TelescopeActionStatus.Error
            return

        expected_next_exposure = datetime.datetime.utcnow() \
            + datetime.timedelta(seconds=self.config['rasa']['exposure'] + 10)

        print('expected next exposure', expected_next_exposure)
        while True:
            self.set_task('Measuring position {} / {}'.format(len(self._focus_measurements) + 1,
                                                              self.config['count']))

            # The wait period rate limits the camera status check
            # The frame received callback will wake this up immedately
            with self._wait_condition:
                self._wait_condition.wait(10)

            # Finished all measurements
            if len(self._focus_measurements) == self.config['count'] or self.aborted:
                break

            # The last measurement has finished - move on to the next
            if current_focus in self._focus_measurements:
                current_focus += self.config['step']
                if not set_focus(self.log_name, self.config['channel'], current_focus,
                                 FOCUS_TIMEOUT):
                    self.status = TelescopeActionStatus.Error
                    return

                if not take_images(self.log_name, self._camera, 1, self.config['rasa']):
                    self.status = TelescopeActionStatus.Error
                    return

                expected_next_exposure = datetime.datetime.utcnow() \
                    + datetime.timedelta(seconds=self.config['rasa']['exposure'] + 10)

            elif datetime.datetime.utcnow() > expected_next_exposure:
                print('Exposure timed out - retrying')
                if not take_images(self.log_name, self._camera, 1, self.config['rasa']):
                    self.status = TelescopeActionStatus.Error
                    return

                expected_next_exposure = datetime.datetime.utcnow() \
                    + datetime.timedelta(seconds=self.config['rasa']['exposure'] + 10)

        # Stop tracking for the next action
        if not tel_stop(self.log_name):
            self.status = TelescopeActionStatus.Error
            return

        if self.aborted or self._acquired_images == self.config['count']:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def received_frame(self, headers):
        """Received a frame from the pipeline"""
        print(headers)
        with self._wait_condition:
            try:
                self._focus_measurements[headers['FOCPOS']] = (headers['MEDFWHM'],
                                                               headers['FWHMCNT'])
            except Exception as e:
                print('failed to update focus measurements')
                print(e)

            self._wait_condition.notify_all()

    def abort(self):
        """Aborted by a weather alert or user action"""
        super().abort()

        tel_stop(self.log_name)
        stop_camera(self.log_name, self._camera)
        stop_focus(self.log_name)

        with self._wait_condition:
            self._wait_condition.notify_all()
