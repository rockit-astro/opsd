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

"""Telescope action to take a set of images without controlling the telescope"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import threading
import Pyro4
from warwick.observatory.common import (
    daemons,
    log)
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)
from warwick.rasa.camera import (
    CameraStatus,
    configure_validation_schema as camera_schema)
from warwick.rasa.pipeline import (
    configure_standard_validation_schema as pipeline_schema)

from .camera_helpers import take_images, get_camera_status

VALID_CAMERA_STATES = [CameraStatus.Acquiring, CameraStatus.Reading, CameraStatus.Waiting]

class ImageSet(TelescopeAction):
    """Telescope action to take a set of images without controlling the telescope"""
    def __init__(self, config):
        super().__init__('Take Image Set', config)
        self._acquired_images = 0
        self._wait_condition = threading.Condition()

    @classmethod
    def validation_schema(cls):
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['count'],
            'properties': {
                'type': {'type': 'string'},
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

        self.set_task('Preparing camera')

        try:
            with daemons.rasa_pipeline.connect() as pipeline:
                pipeline.configure(self.config['pipeline'])
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

        if not take_images(self.log_name, self.config['count'], self.config['rasa']):
            log.error(self.log_name, 'Aborting action')
            print('Aborting action')
            self.status = TelescopeActionStatus.Error
            return

        while True:
            self.set_task('Acquiring image {} / {}'.format(self._acquired_images + 1,
                                                           self.config['count']))

            # The wait period rate limits the camera status check
            # The frame received callback will wake this up immedately
            # TODO: This needs to be rewritten in terms of a timeout
            # otherwise it may check while the pipeline is processing and fail
            with self._wait_condition:
                self._wait_condition.wait(60)

            if self._acquired_images == self.config['count'] or self.aborted:
                break

            # Check camera for error status
            status = get_camera_status(self.log_name)
            if not status:
                print('Failed to query camera status')
                log.error(self.log_name, 'Failed to query camera status')
                break

            if status['state'] not in VALID_CAMERA_STATES:
                message = 'Camera is in unexpected state', CameraStatus.label(status['state'])
                print(message)
                log.error(self.log_name, message)

                if status['state'] == CameraStatus.Idle:
                    remaining = self.config['count'] - self._acquired_images
                    message = 'Restarting remaining {} exposures'.format(remaining)
                    print(message)
                    log.info(self.log_name, message)

                    if not take_images(self.log_name, remaining, self.config['rasa']):
                        print('Aborting action')
                        log.error(self.log_name, 'Aborting action')

                        self.status = TelescopeActionStatus.Error
                        return

                    continue
                break

        if self.aborted or self._acquired_images == self.config['count']:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def received_frame(self, headers):
        """Received a frame from the pipeline"""
        with self._wait_condition:
            self._acquired_images += 1
            self._wait_condition.notify_all()

    def abort(self):
        """Aborted by a weather alert or user action"""
        super().abort()

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
