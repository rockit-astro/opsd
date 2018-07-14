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
import Pyro4
from warwick.observatory.common import (
    daemons,
    log)
from warwick.rasa.camera import (
    CommandStatus as CamCommandStatus,
    configure_validation_schema as camera_schema)
from warwick.rasa.focuser import CommandStatus as FocCommandStatus
from warwick.rasa.telescope import CommandStatus as TelCommandStatus
from warwick.rasa.pipeline import (
    configure_standard_validation_schema as pipeline_schema)

from . import TelescopeAction, TelescopeActionStatus

SLEW_TIMEOUT = 120
FOCUS_TIMEOUT = 300

def __set_focus(channel, position, log_name):
    try:
        with daemons.rasa_focus.connect(timeout=FOCUS_TIMEOUT) as focusd:
            status = focusd.set_focus(channel, position)
            if status != FocCommandStatus.Succeeded:
                print('Failed to set focuser position')
                log.error(log_name, 'Failed to set focuser position')
                return False
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with focuser daemon')
        log.error(log_name, 'Failed to communicate with focuser daemon')
        return False
    except Exception as e:
        print('Unknown error while configuring focuser')
        print(e)
        log.error(log_name, 'Unknown error while configuring focuser')
        return False
    return True

def __start_exposure(config, log_name):
    try:
        with daemons.rasa_camera.connect() as cam:

            if config:
                status = cam.configure(config)

            if not config or status == CamCommandStatus.Succeeded:
                status = cam.start_sequence(1)

            if status != CamCommandStatus.Succeeded:
                print('Failed to start exposure sequence')
                log.error(log_name, 'Failed to start exposure sequence')
                return False
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with camera daemon')
        log.error(log_name, 'Failed to communicate with camera daemon')
        return False
    except Exception as e:
        print('Unknown error with camera')
        print(e)
        log.error(log_name, 'Unknown error with camera')
        return False

class FocusSweep(TelescopeAction):
    """Telescope action to do a focus sweep on a defined field"""
    def __init__(self, config):
        super().__init__('Focus Sweep', config)
        self._acquired_images = 0
        self._wait_condition = threading.Condition()
        self._focus_measurements = {}

    @classmethod
    def validation_schema(cls):
        # TODO: This will need to be generalized to support two focusers in the future
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['ra', 'dec', 'start', 'step', 'count'],
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
        try:
            with daemons.rasa_telescope.connect(timeout=SLEW_TIMEOUT) as teld:
                status = teld.track_radec(math.radians(self.config['ra']),
                                          math.radians(self.config['dec']))
                if not self.aborted and status != TelCommandStatus.Succeeded:
                    print('Failed to slew telescope')
                    log.error(self.log_name, 'Failed to slew telescope')
                    self.status = TelescopeActionStatus.Error
                    return
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with telescope daemon')
            log.error(self.log_name, 'Failed to communicate with telescope daemon')
            self.status = TelescopeActionStatus.Error
            return
        except Exception as e:
            print('Unknown error while slewing telescope')
            print(e)
            log.error(self.log_name, 'Unknown error while slewing telescope')
            self.status = TelescopeActionStatus.Error
            return

        if self.aborted:
            self.status = TelescopeActionStatus.Error
            return

        self.set_task('Preparing camera')

        try:
            pipeline_config = {}
            pipeline_config.update(self.config['pipeline'])
            pipeline_config.update({
                'fwhm': True,
                'type': 'SCIENCE',
                'object': 'Focus run',
            })

            with daemons.rasa_pipeline.connect() as pipeline:
                pipeline.configure(pipeline_config)
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with pipeline daemon')
            log.error(self.log_name, 'Failed to communicate with pipeline daemon')
            self.status = TelescopeActionStatus.Error
            return
        except Exception as e:
            print('Unknown error while configuring pipeline')
            print(e)
            log.error(self.log_name, 'Unknown error while configuring pipeline')
            self.status = TelescopeActionStatus.Error
            return

        # Move focuser to the start of the focus range
        current_focus = self.config['start']

        if not __set_focus(self.config['channel'], current_focus, self.log_name):
            self.status = TelescopeActionStatus.Error
            return

        # Configure the camera then take the first exposure to start the process
        if not __start_exposure(self.config['rasa'], self.log_name):
            self.status = TelescopeActionStatus.Error
            return

        expected_next_exposure = datetime.datetime.utcnow() \
            + datetime.timedelta(seconds=self.config['rasa']['exposure'] + 10)

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
                if not __set_focus(self.config['channel'], current_focus, self.log_name):
                    self.status = TelescopeActionStatus.Error
                    return

                if not __start_exposure(self.config['rasa'], self.log_name):
                    self.status = TelescopeActionStatus.Error
                    return

                expected_next_exposure = datetime.datetime.utcnow() \
                    + datetime.timedelta(seconds=self.config['rasa']['exposure'] + 10)

            elif datetime.datetime.utcnow() > expected_next_exposure:
                print('Exposure timed out - retrying')
                if not __start_exposure(self.config['rasa'], self.log_name):
                    self.status = TelescopeActionStatus.Error
                    return

                expected_next_exposure = datetime.datetime.utcnow() \
                    + datetime.timedelta(seconds=self.config['rasa']['exposure'] + 10)


        if not self.aborted and self._acquired_images == self.config['count']:
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
        try:
            with daemons.rasa_telescope.connect() as teld:
                teld.stop()
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with telescope daemon')
        except Exception as e:
            print('Unknown error while stopping telescope')
            print(e)

        try:
            with daemons.rasa_camera.connect() as camd:
                camd.stop_sequence()
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with camera daemon')
        except Exception as e:
            print('Unknown error while stopping camera')
            print(e)

        try:
            with daemons.rasa_focus.connect() as focusd:
                focusd.stop_channel(self.config['channel'])
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with focus daemon')
        except Exception as e:
            print('Unknown error while stopping focuser')
            print(e)

        with self._wait_condition:
            self._wait_condition.notify_all()
