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

"""Telescope action to wait for a specified amount of time"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import math
import threading
import Pyro4
from warwick.observatory.common import (
    daemons,
    log)
from warwick.rasa.camera import (
    CameraStatus,
    CommandStatus as CamCommandStatus,
    configure_validation_schema as camera_schema)
from warwick.rasa.telescope import CommandStatus as TelCommandStatus
from warwick.rasa.pipeline import (
    configure_standard_validation_schema as pipeline_schema)

from . import TelescopeAction, TelescopeActionStatus

SLEW_TIMEOUT = 120

class TestHorizonImage(TelescopeAction):
    """Telescope action to power on and prepare the telescope for observing"""
    def __init__(self, config):
        super().__init__('Test Horizon Image', config)
        self._acquired_images = 0
        self._wait_condition = threading.Condition()

    @classmethod
    def validation_schema(cls):
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['alt', 'az', 'count'],
            'properties': {
                'type': {'type': 'string'},
                'alt': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': math.pi / 2
                },
                'az': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 2 * math.pi
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
        self.set_task('Waiting before slew')
        with self._wait_condition:
            self._wait_condition.wait(5)
        if self.aborted:
            self.status = TelescopeActionStatus.Error
            return

        self.set_task('Slewing')
        try:
            with daemons.rasa_telescope.connect(timeout=SLEW_TIMEOUT) as teld:
                status = teld.slew_altaz(self.config['alt'], self.config['az'])
                if not self.aborted and status != TelCommandStatus.Succeeded:
                    print('Failed to slew telescope')
                    log.error('opsd', 'Failed to slew telescope')
                    self.status = TelescopeActionStatus.Error
                    return
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with telescope daemon')
            log.error('opsd', 'Failed to communicate with telescope daemon')
            self.status = TelescopeActionStatus.Error
            return
        except Exception as e:
            print('Unknown error while slewing telescope')
            print(e)
            log.error('opsd', 'Unknown error while slewing telescope')
            self.status = TelescopeActionStatus.Error
            return

        if self.aborted:
            self.status = TelescopeActionStatus.Error
            return

        self.set_task('Preparing camera')

        try:
            with daemons.rasa_pipeline.connect() as pipeline:
                pipeline.configure(self.config['pipeline'])
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with pipeline daemon')
            log.error('opsd', 'Failed to communicate with pipeline daemon')
            self.status = TelescopeActionStatus.Error
            return
        except Exception as e:
            print('Unknown error while configuring pipeline')
            print(e)
            log.error('opsd', 'Unknown error while configuring pipeline')
            self.status = TelescopeActionStatus.Error
            return

        try:
            with daemons.rasa_camera.connect() as cam:
                status = cam.configure(self.config['rasa'])
                if status == CamCommandStatus.Succeeded:
                    status = cam.start_sequence(self.config['count'])

                if status != CamCommandStatus.Succeeded:
                    print('Failed to start exposure sequence')
                    log.error('opsd', 'Failed to start exposure sequence')
                    self.status = TelescopeActionStatus.Error
                    return
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with camera daemon')
            log.error('opsd', 'Failed to communicate with camera daemon')
            self.status = TelescopeActionStatus.Error
            return
        except Exception as e:
            print('Unknown error with camera')
            print(e)
            log.error('opsd', 'Unknown error with camera')
            self.status = TelescopeActionStatus.Error
            return

        while True:
            self.set_task('Acquiring image {} / {}'.format(self._acquired_images + 1,
                                                           self.config['count']))

            # The wait period rate limits the camera status check
            # The frame received callback will wake this up immedately
            with self._wait_condition:
                self._wait_condition.wait(10)

            if self._acquired_images == self.config['count'] or self.aborted:
                break

            # Check camera for error status
            try:
                with daemons.rasa_camera.connect() as camd:
                    status = camd.report_status()
            except Pyro4.errors.CommunicationError:
                print('Failed to communicate with camera daemon')
                break
            except Exception as e:
                print('Unknown error while stopping camera')
                print(e)
                break

            if status['state'] not in [CameraStatus.Acquiring, CameraStatus.Reading]:
                print('Camera is in unexpected state', CameraStatus.label(status['state']))
                break

        if not self.aborted and self._acquired_images == self.config['count']:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def received_frame(self, headers):
        """Received a frame from the pipeline"""
        print(headers)
        with self._wait_condition:
            self._acquired_images += 1
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

        with self._wait_condition:
            self._wait_condition.notify_all()
