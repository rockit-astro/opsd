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


"""Telescope action to warm and power off the cameras"""

# pylint: disable=too-many-branches

import sys
import threading
import traceback
import Pyro4
from astropy.time import Time
from rockit.common import daemons, log, validation
from rockit.lmount import MountState
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cameras, cam_configure, cam_status, cam_stop, cam_is_warm, cam_shutdown
from .mount_helpers import mount_status, mount_park

CAMERA_SHUTDOWN_TIMEOUT = 10
CAMERA_STOP_TIMEOUT = 30

# Interval (in seconds) to poll the camera for temperature lock
CAMERA_CHECK_INTERVAL = 10


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
        if 'start' in config:
            self._start_date = Time(config['start'])
        else:
            self._start_date = None

        if 'cameras' in config:
            self._camera_ids = config['cameras']
        else:
            self._camera_ids = cameras.keys()

        self._wait_condition = threading.Condition()

    def run_thread(self):
        """Thread that runs the hardware actions"""
        if self._start_date is not None and Time.now() < self._start_date:
            self.set_task(f'Waiting until {self._start_date.strftime("%H:%M:%S")}')
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        status = mount_status(self.log_name)
        if status and 'state' in status and status['state'] != MountState.Disabled:
            self.set_task('Parking mount')
            mount_park(self.log_name)

        # Warm cameras
        self.set_task('Warming cameras')
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
            # Power cameras off
            self.set_task('Disabling cameras')
            for camera_id in self._camera_ids:
                cam_shutdown(self.log_name, camera_id)

            try:
                with daemons.clasp_power.connect() as powerd:
                    p = powerd.last_measurement()
                    for camera_id in self._camera_ids:
                        if camera_id in p and p[camera_id]:
                            powerd.switch(camera_id, False)
            except Pyro4.errors.CommunicationError:
                log.error(self.log_name, 'Failed to communicate with power daemon')
            except Exception:
                log.error(self.log_name, 'Unknown error with power daemon')
                traceback.print_exc(file=sys.stdout)

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
