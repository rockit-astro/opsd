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

import sys
import threading
import traceback
import Pyro4
from astropy.time import Time
from rockit.common import daemons, log, validation
from rockit.lmount import MountState
from rockit.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.camera.qhy import CameraStatus, CoolerMode
from .camera_helpers import cam_configure, cam_status, cam_stop
from .mount_helpers import mount_status, mount_park

CAMERA_SHUTDOWN_TIMEOUT = 10
CAMERA_STOP_TIMEOUT = 30

# Interval (in seconds) to poll the camera for temperature lock
CAMERA_CHECK_INTERVAL = 10


class ShutdownCamera(TelescopeAction):
    """
    Telescope action to park the mount and warm then power off the cameras.

    Example block:
    {
        "type": "ShutdownCamera",
        "start": "2022-09-18T22:20:00", # Optional: defaults to immediately
    }
    """
    def __init__(self, log_name, config):
        super().__init__('Shutdown Camera', log_name, config)
        if 'start' in config:
            self._start_date = Time(config['start'])
        else:
            self._start_date = None

        self._wait_condition = threading.Condition()

    def __shutdown_camera(self):
        """Disables a given camera"""
        try:
            with daemons.halfmetre_cam.connect(timeout=CAMERA_SHUTDOWN_TIMEOUT) as cam:
                cam.shutdown()
        except Pyro4.errors.CommunicationError:
            log.error(self.log_name, 'Failed to communicate with camera')
            return False
        except Exception:
            log.error(self.log_name, 'Unknown error with camera')
            traceback.print_exc(file=sys.stdout)
            return False
        return True

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
        cam_stop(self.log_name, timeout=CAMERA_STOP_TIMEOUT)
        cam_configure(self.log_name, {'temperature': None}, quiet=True)

        warm = False
        while not self.aborted:
            if warm:
                continue

            status = cam_status(self.log_name)
            if status['state'] == CameraStatus.Disabled:
                break
            if 'cooler_mode' not in status:
                log.error(self.log_name, 'Failed to check temperature on camera')
                break
            if status['cooler_mode'] == CoolerMode.Warm:
                break

            with self._wait_condition:
                self._wait_condition.wait(CAMERA_CHECK_INTERVAL)

        if not self.aborted:
            # Power camera off
            self.set_task('Disabling camera')
            self.__shutdown_camera()

            try:
                with daemons.halfmetre_power.connect() as powerd:
                    p = powerd.last_measurement()
                    if p['camera']:
                        powerd.switch('camera', False)
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
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                }
            }
        })