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

"""Telescope action to power on and prepare the telescope for observing"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods

import sys
import threading
import traceback
import Pyro4
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import log
from warwick.observatory.camera.andor import CommandStatus as CamCommandStatus
from .telescope_helpers import tel_status, tel_init, tel_home, tel_park
from .camera_helpers import cameras, cam_status

CAM_INIT_TIMEOUT = 30

# Interval (in seconds) to poll the camera for temperature lock
CAMERA_CHECK_INTERVAL = 10

class Initialize(TelescopeAction):
    """Telescope action to power on and prepare the telescope for observing"""
    def __init__(self, log_name):
        super().__init__('Initializing', log_name, {})

        self._cooling_condition = threading.Condition()

    def __initialize_camera(self, camera_id):
        """Initializes a given camera and enables cooling"""
        try:
            self.set_task('Initializing Cameras')
            with cameras[camera_id].connect(timeout=CAM_INIT_TIMEOUT) as cam:
                status = cam.initialize()
                if status not in [CamCommandStatus.Succeeded,
                                  CamCommandStatus.CameraNotUninitialized]:
                    log.error(self.log_name, 'Failed to initialize camera ' + camera_id)
                    return False

                # Calling configure with an empty dictionary resets everything to defaults
                if cam.configure({}, quiet=True) != CamCommandStatus.Succeeded:
                    log.error(self.log_name, 'Failed to reset camera ' + camera_id + ' to defaults')
                    return False
        except Pyro4.errors.CommunicationError:
            log.error(self.log_name, 'Failed to communicate with camera ' + camera_id)
            return False
        except Exception:
            log.error(self.log_name, 'Unknown error with camera ' + camera_id)
            traceback.print_exc(file=sys.stdout)
            return False
        return True

    def __wait_for_temperature_lock(self):
        """
        Waits until all cameras have reached their target temperature
        Returns True on success, False on error
        """
        # Wait for cameras to cool if required
        self.set_task('Cooling cameras')
        locked = {k: False for k in cameras}

        while not self.aborted:
            for camera_id in cameras:
                status = cam_status(self.log_name, camera_id)
                if 'temperature_locked' not in status:
                    log.error(self.log_name, 'Failed to check temperature on camera ' + camera_id)
                    return False

                locked[camera_id] = status['temperature_locked']

            if all([locked[k] for k in locked]):
                break

            with self._cooling_condition:
                self._cooling_condition.wait(CAMERA_CHECK_INTERVAL)
        return not self.aborted

    def __initialize_telescope(self):
        """Initializes and homes the telescope"""
        self.set_task('Initializing Mount')

        if not tel_init(self.log_name):
            log.error(self.log_name, 'Failed to initialize mount')
            return False

        status = tel_status(self.log_name)
        if not status.get('axes_homed', False):
            self.set_task('Homing Mount')
            if not tel_home(self.log_name):
                log.error(self.log_name, 'Failed to home mount')
                return False

        self.set_task('Slewing to park position')
        return tel_park(self.log_name)

    def run_thread(self):
        """Thread that runs the hardware actions"""
        for camera_id in cameras:
            if not self.__initialize_camera(camera_id):
                self.status = TelescopeActionStatus.Error
                return

        if not self.__initialize_telescope():
            self.status = TelescopeActionStatus.Error
            return

        locked = self.__wait_for_temperature_lock()
        if self.aborted or locked:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        # Aborting while homing isn't a good idea
        # so we only abort the wait for temperature lock
        with self._cooling_condition:
            self._cooling_condition.notify_all()
