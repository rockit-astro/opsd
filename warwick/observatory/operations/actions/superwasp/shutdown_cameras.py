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
import astropy.units as u
from warwick.observatory.common import daemons, validation
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import log
from warwick.observatory.camera.qhy import CameraStatus, CoolerMode
from warwick.observatory.talon import TelState
from .camera_helpers import cameras, cam_configure, cam_status, cam_stop
from .telescope_helpers import tel_status, tel_stop, tel_park

CAMERA_SHUTDOWN_TIMEOUT = 10
CAMERA_STOP_TIMEOUT = 30

# Interval (in seconds) to poll the camera for temperature lock
CAMERA_CHECK_INTERVAL = 10

CONFIG_SCHEMA = {
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
}


class ShutdownCameras(TelescopeAction):
    """Telescope action to warm and power off the camereas"""
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

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        return validation.validation_errors(config_json, CONFIG_SCHEMA)

    def __wait_until_or_aborted(self, target_time):
        """
        Wait until a specified time or the action has been aborted
        :param target: Astropy time to wait for
        :return: True if the time has been reached, false if aborted
        """
        while True:
            remaining = target_time - Time.now()
            if remaining < 0 or self.aborted:
                break

            with self._wait_condition:
                self._wait_condition.wait(min(10, remaining.to(u.second).value))

        return not self.aborted

    def __shutdown_camera(self, camera_id):
        """Disables a given camera"""
        try:
            with cameras[camera_id].connect(timeout=CAMERA_SHUTDOWN_TIMEOUT) as cam:
                cam.shutdown()
        except Pyro4.errors.CommunicationError:
            log.error(self.log_name, 'Failed to communicate with camera ' + camera_id)
            return False
        except Exception:
            log.error(self.log_name, 'Unknown error with camera ' + camera_id)
            traceback.print_exc(file=sys.stdout)
            return False
        return True

    def run_thread(self):
        """Thread that runs the hardware actions"""
        if self._start_date is not None and Time.now() < self._start_date:
            self.set_task(f'Waiting until {self._start_date.strftime("%H:%M:%S")}')
            self.__wait_until_or_aborted(self._start_date)

        status = tel_status(self.log_name)
        if status and 'state' in status and status['state'] != TelState.Absent:
            self.set_task('Parking telescope')
            tel_stop(self.log_name)
            tel_park(self.log_name)

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
                if status['state'] == CameraStatus.Disabled:
                    warm[camera_id] = True
                elif 'cooler_mode' not in status:
                    log.error(self.log_name, 'Failed to check temperature on camera ' + camera_id)
                    warm[camera_id] = True
                else:
                    warm[camera_id] = status['cooler_mode'] == CoolerMode.Warm

            if all(warm[k] for k in warm):
                break

            with self._wait_condition:
                self._wait_condition.wait(CAMERA_CHECK_INTERVAL)

        if not self.aborted:
            # Power cameras off
            self.set_task('Disabling cameras')
            for camera_id in self._camera_ids:
                self.__shutdown_camera(camera_id)

            try:
                with daemons.superwasp_power.connect() as powerd:
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