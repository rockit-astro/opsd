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

import threading
import Pyro4
from warwick.observatory.common import (
    daemons,
    log)
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)
from warwick.rasa.camera import CommandStatus as CamCommandStatus
from warwick.rasa.operations.actions.telescope_helpers import tel_park_stow
from warwick.rasa.telescope import CommandStatus as TelCommandStatus

HOME_TIMEOUT = 120

CAM_INIT_TIMEOUT = 30

# Interval (in seconds) to poll the camera for temperature lock
CAMERA_CHECK_INTERVAL = 10

class Initialize(TelescopeAction):
    """Telescope action to power on and prepare the telescope for observing"""
    def __init__(self):
        super().__init__('Initializing', {})
        self._camera_daemons = {
            'RASA': daemons.rasa_camera
        }

        self._cooling_condition = threading.Condition()

    def __initialize_camera(self, name, daemon):
        """Initializes a given camera and enables cooling"""
        try:
            self.set_task('Initializing Cameras')
            with daemon.connect(timeout=CAM_INIT_TIMEOUT) as cam:
                status = cam.initialize()
                if status not in [CamCommandStatus.Succeeded,
                                  CamCommandStatus.CameraNotUninitialized]:
                    print('Failed to initialize ' + name + ' camera')
                    log.error(self.log_name, 'Failed to initialize ' + name + ' camera')
                    return False

                # Calling configure with an empty dictionary resets everything to defaults
                if cam.configure({}, quiet=True) != CamCommandStatus.Succeeded:
                    print('Failed to reset ' + name + ' camera to defaults')
                    log.error(self.log_name, 'Failed to reset ' + name + ' camera to defaults')
                    return False

        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with ' + name + ' camera daemon')
            log.error(self.log_name, 'Failed to communicate with ' + name + ' camera daemon')
            return False
        except Exception as e:
            print('Unknown error with ' + name + ' camera')
            print(e)
            log.error(self.log_name, 'Unknown error with ' + name + ' camera')
            return False
        return True

    def __wait_for_temperature_lock(self):
        """Waits until both cameras have reached their target temperature
           Returns True on success, False on error
        """
        # Wait for cameras to cool if required
        self.set_task('Cooling cameras')
        locked = {k: False for k in self._camera_daemons}

        while not self.aborted:
            try:
                for arm in self._camera_daemons:
                    with self._camera_daemons[arm].connect() as cam:
                        status = cam.report_status()
                        if 'temperature_locked' not in status:
                            print('Failed to check tempearture on ' + arm + ' camera')
                            log.error(self.log_name, 'Failed to check temperature on ' + arm +
                                      ' camera')
                            return False

                        locked[arm] = status['temperature_locked']
            except Pyro4.errors.CommunicationError:
                print('Failed to communicate with ' + arm + ' camera daemon')
                log.error(self.log_name, 'Failed to communicate with ' + arm + ' camera daemon')
                return False
            except Exception as e:
                print('Unknown error with ' + arm + ' camera')
                print(e)
                log.error(self.log_name, 'Unknown error with ' + arm + ' camera')
                return False

            if all([locked[k] for k in locked]):
                break

            with self._cooling_condition:
                self._cooling_condition.wait(CAMERA_CHECK_INTERVAL)
        return not self.aborted

    def __initialize_telescope(self):
        """Initializes and homes the telescope"""
        try:
            self.set_task('Initializing Telescope')

            with daemons.rasa_telescope.connect(timeout=HOME_TIMEOUT) as teld:
                status = teld.initialize()
                if status not in [TelCommandStatus.Succeeded,
                                  TelCommandStatus.TelescopeNotDisabled]:
                    print('Failed to initialize telescope')
                    log.error(self.log_name, 'Failed to initialize telescope')
                    return False

            self.set_task('Slewing to park position')
            if not tel_park_stow(self.log_name):
                return False

        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with telescope daemon')
            log.error(self.log_name, 'Failed to communicate with telescope daemon')
            return False
        except Exception as e:
            print('Unknown error with telescope')
            print(e)
            log.error(self.log_name, 'Unknown error with telescope')
            return False
        return True

    def run_thread(self):
        """Thread that runs the hardware actions"""
        for arm in self._camera_daemons:
            if not self.__initialize_camera(arm, self._camera_daemons[arm]):
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
