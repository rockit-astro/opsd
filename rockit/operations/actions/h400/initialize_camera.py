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

"""Telescope action to power on and cool the cameras"""
import sys
import threading
import traceback
import Pyro4
from astropy.time import Time
import astropy.units as u
from rockit.camera.moravian import CommandStatus as CamCommandStatus
from rockit.common import daemons, log, validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cam_status

CAMERA_POWERON_DELAY = 5
CAMERA_INIT_TIMEOUT = 60

# Interval (in seconds) to poll the camera for temperature lock
CAMERA_CHECK_INTERVAL = 10

# Exit with an error if temperatures haven't locked after this many seconds
CAMERA_COOLING_TIMEOUT = 900


class Progress:
    Waiting, Initalizing, Cooling = range(3)


class InitializeCamera(TelescopeAction):
    """
    Telescope action to power on and cool the cameras.

    Example block:
    {
        "type": "InitializeCamera",
        "start": "2022-09-18T22:20:00", # Optional: defaults to immediately
    }
    """
    def __init__(self, log_name, config):
        super().__init__('Initializing Camera', log_name, config)

        self._progress = Progress.Waiting
        if 'start' in config:
            self._start_date = Time(config['start'])
        else:
            self._start_date = None

        self._wait_condition = threading.Condition()

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []
        if self._progress <= Progress.Waiting and self._start_date:
            tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')

        if self._progress <= Progress.Initalizing:
            tasks.append('Initialize camera')

        if self._progress <= Progress.Cooling:
            tasks.append('Wait for temperature lock')

        return tasks

    def __initialize_camera(self):
        """Initializes a given camera and resets configuration"""
        try:
            with daemons.halfmetre_cam.connect(timeout=CAMERA_INIT_TIMEOUT) as cam:
                status = cam.initialize()
                if status not in [CamCommandStatus.Succeeded,
                                  CamCommandStatus.CameraNotUninitialized]:
                    log.error(self.log_name, 'Failed to initialize camera')
                    return False

                if cam.configure({}, quiet=True) != CamCommandStatus.Succeeded:
                    log.error(self.log_name, 'Failed to reset camera to defaults')
                    return False
        except Pyro4.errors.CommunicationError:
            log.error(self.log_name, 'Failed to communicate with camera')
            return False
        except Exception:
            log.error(self.log_name, 'Unknown error with camera')
            traceback.print_exc(file=sys.stdout)
            return False
        return True

    def __wait_for_temperature_lock(self):
        """Waits until all cameras have reached their target temperature
           Returns True on success, False on error
        """
        # Wait for cameras to cool if required
        start = Time.now()
        while not self.aborted:
            if (Time.now() - start) > CAMERA_COOLING_TIMEOUT * u.s:
                return False

            status = cam_status(self.log_name)
            if 'temperature_locked' not in status:
                log.error(self.log_name, 'Failed to check temperature on camera')
                return False

            if status['temperature_locked']:
                break

            with self._wait_condition:
                self._wait_condition.wait(CAMERA_CHECK_INTERVAL)
        return not self.aborted

    def run_thread(self):
        """Thread that runs the hardware actions"""
        if self._start_date is not None and Time.now() < self._start_date:
            if not self.wait_until_time_or_aborted(self._start_date, self._wait_condition):
                self.status = TelescopeActionStatus.Complete
                return

        self._progress = Progress.Initalizing

        # Power camera on if needed
        switched = False
        try:
            with daemons.halfmetre_power.connect() as powerd:
                p = powerd.last_measurement()
                if not p['camera']:
                    switched = True
                    powerd.switch('camera', True)
        except Pyro4.errors.CommunicationError:
            log.error(self.log_name, 'Failed to communicate with power daemon')
        except Exception:
            log.error(self.log_name, 'Unknown error with power daemon')
            traceback.print_exc(file=sys.stdout)

        if switched:
            # Wait for camera to power up
            with self._wait_condition:
                self._wait_condition.wait(CAMERA_POWERON_DELAY)

        if not self.__initialize_camera():
            self.status = TelescopeActionStatus.Error
            return

        self._progress = Progress.Cooling
        locked = self.__wait_for_temperature_lock()
        if self.aborted or locked:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

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
            'properties': {
                'type': {'type': 'string'},

                # Optional
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                }
            }
        })
