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

"""Telescope action to warm and power off the cameras"""

# pylint: disable=too-many-branches

import threading
from astropy.time import Time
from rockit.common import validation
from rockit.mount.planewave import MountState
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import (cameras, cam_configure, cam_status, cam_stop, cam_is_warm,
                             cam_shutdown, cam_switch_power, das_machines, cam_shutdown_vms)
from .mount_helpers import mount_status, mount_park

CAMERA_SHUTDOWN_TIMEOUT = 10
CAMERA_STOP_TIMEOUT = 30

# Interval (in seconds) to poll the camera for temperature lock
CAMERA_CHECK_INTERVAL = 10


class Progress:
    Waiting, Parking, Warming, ShuttingDown, ShuttingDownVMs = range(5)


class ShutdownCameras(TelescopeAction):
    """
    Telescope action to park the mount and warm then power off the cameras.

    Example block:
    {
        "type": "ShutdownCameras",
        "start": "2022-09-18T22:20:00", # Optional: defaults to immediately
        "cameras": ["cam1"] # Optional: defaults to all cameras
    }
    """
    def __init__(self, log_name, config):
        super().__init__('Shutdown Cameras', log_name, config)
        self._progress = Progress.Waiting
        if 'start' in config:
            self._start_date = Time(config['start'])
        else:
            self._start_date = None

        if 'cameras' in config:
            self._camera_ids = config['cameras']
            self._das_ids = []
            for das_id, das_info in das_machines.items():
                if all(camera_id in self._camera_ids for camera_id in das_info['cameras']):
                    self._das_ids.append(das_id)
        else:
            self._camera_ids = cameras.keys()
            self._das_ids = das_machines.keys()

        self._wait_condition = threading.Condition()

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []
        if self._progress <= Progress.Waiting and self._start_date:
            tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')

        if self._progress <= Progress.Parking:
            tasks.append('Park mount')

        if self._progress <= Progress.Warming:
            tasks.append(f'Warm cameras ({", ".join(self._camera_ids)})')

        if self._progress <= Progress.ShuttingDown:
            tasks.append(f'Shutdown cameras ({", ".join(self._camera_ids)})')

        if self._progress <= Progress.ShuttingDownVMs:
            tasks.append(f'Shutdown DAS VMs ({", ".join(self._das_ids)})')

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        if self._start_date is not None and Time.now() < self._start_date:
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        self._progress = Progress.Parking

        status = mount_status(self.log_name)
        if status and 'state' in status and status['state'] not in [MountState.Disabled, MountState.Parked]:
            mount_park(self.log_name)

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
                warm[camera_id] = cam_is_warm(self.log_name, camera_id, status)

            if all(warm[k] for k in warm):
                break

            with self._wait_condition:
                self._wait_condition.wait(CAMERA_CHECK_INTERVAL)

        if not self.aborted:
            self._progress = Progress.ShuttingDown
            for camera_id in self._camera_ids:
                cam_shutdown(self.log_name, camera_id)

            cam_switch_power(self.log_name, self._camera_ids, False)

        if not self.aborted:
            self._progress = Progress.ShuttingDownVMs
            cam_shutdown_vms(self.log_name, self._das_ids)

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
