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

"""Telescope action to park the mount, warm then power off the cameras"""

# pylint: disable=too-many-branches

import threading
from astropy.time import Time
from rockit.camera.qhy import CameraStatus, CoolerMode
from rockit.common import log, validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cameras, cam_configure, cam_status, cam_stop, cam_shutdown

CAMERA_SHUTDOWN_TIMEOUT = 10
CAMERA_STOP_TIMEOUT = 30

# Interval (in seconds) to poll the camera for temperature lock
CAMERA_CHECK_INTERVAL = 10


class Progress:
    Waiting, Warming, ShuttingDown = range(3)


class ShutdownCameras(TelescopeAction):
    """
    Telescope action to warm then power off the cameras.

    Example block:
    {
        "type": "ShutdownCameras",
        "start": "2022-09-18T22:20:00", # Optional: defaults to immediately
        "cameras": ["cam1"] # Optional: defaults to all cameras
    }
    """
    def __init__(self, **args):
        super().__init__('Shutdown Cameras', **args)
        self._progress = Progress.Waiting
        if 'start' in self.config:
            self._start_date = Time(self.config['start'])
        else:
            self._start_date = None

        if 'cameras' in self.config:
            self._camera_ids = self.config['cameras']
        else:
            self._camera_ids = cameras.keys()

        self._wait_condition = threading.Condition()

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []
        if self._progress <= Progress.Waiting and self._start_date:
            tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')

        if self._progress <= Progress.Warming:
            tasks.append(f'Warm cameras ({", ".join(self._camera_ids)})')

        if self._progress <= Progress.ShuttingDown:
            tasks.append(f'Shutdown cameras ({", ".join(self._camera_ids)})')

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        if self._start_date is not None and Time.now() < self._start_date:
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        # Warm cameras
        self._progress = Progress.Warming
        for camera_id in self._camera_ids:
            cam_stop(self.log_name, camera_id, timeout=CAMERA_STOP_TIMEOUT)
            cam_configure(self.log_name, camera_id, {'temperature': None}, quiet=True)

        warm = {camera_id: False for camera_id in self._camera_ids}
        while not self.aborted:
            for camera_id in self._camera_ids:
                if warm[camera_id]:
                    continue

                status = cam_status(self.log_name, camera_id)
                if 'state' not in status or 'cooler_mode' not in status:
                    log.error(self.log_name, 'Failed to check temperature on camera ' + camera_id)
                    warm[camera_id] = True
                else:
                    warm[camera_id] = status['state'] == CameraStatus.Disabled or \
                                      status['cooler_mode'] == CoolerMode.Warm

            if all(warm[k] for k in warm):
                break

            with self._wait_condition:
                self._wait_condition.wait(CAMERA_CHECK_INTERVAL)

        if not self.aborted:
            self._progress = Progress.ShuttingDown
            for camera_id in self._camera_ids:
                cam_shutdown(self.log_name, camera_id)

        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        with self._wait_condition:
            self._wait_condition.notify_all()

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        return validation.validation_errors(config_json, {
            'type': 'object',
            'additionalProperties': False,
            'required': [],
            'properties': {
                'type': {'type': 'string'},

                # Optional
                'cameras': {
                    'type': 'array',
                    'items': {
                        'type': 'string',
                        'enum': cameras.keys()
                    }
                },

                # Optional
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                }
            }
        })
