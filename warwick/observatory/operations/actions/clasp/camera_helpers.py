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

"""Helper functions for actions to interact with the cameras"""

# pylint: disable=too-many-return-statements

import sys
import time
import traceback
from astropy.time import Time
import astropy.units as u
import Pyro4
from rockit.common import daemons, log
from warwick.observatory.camera.qhy import (
    CameraStatus as QHYStatus,
    CommandStatus as QHYCommandStatus,
    CoolerMode as QHYCoolerMode)
from warwick.observatory.camera.raptor import (
    CameraStatus as SWIRStatus,
    CommandStatus as SWIRCommandStatus,
    CoolerMode as SWIRCoolerMode)

cameras = {
    'cam1': daemons.clasp_camera_1,
    'cam2': daemons.clasp_camera_2,
}


def cam_configure(log_name, camera_id, config, quiet=False):
    """Set camera configuration
       config is assumed to contain a dictionary of camera
       configuration that has been validated by the camera schema.
    """
    try:
        with cameras[camera_id].connect() as cam:
            if camera_id == 'cam2':
                status = cam.configure(config, quiet=quiet)
                if status == SWIRCommandStatus.Succeeded:
                    return True

                if status == SWIRCommandStatus.CameraNotInitialized:
                    log.error(log_name, f'Camera {camera_id} is not initialized')
                    return False
            else:
                status = cam.configure(config, quiet=quiet)
                if status == QHYCommandStatus.Succeeded:
                    return True

                if status == QHYCommandStatus.CameraNotInitialized:
                    log.error(log_name, f'Camera {camera_id} is not initialized')
                    return False

            log.error(log_name, f'Failed to configure camera {camera_id} with status {status}')
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
    except Exception:
        log.error(log_name, 'Unknown error with camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
    return False


def cam_take_images(log_name, camera_id, count=1, config=None, quiet=False):
    """Start an exposure sequence with count images

       If config is non-None it is assumed to contain
       a dictionary of camera configuration that has been
       validated by the camera schema, which is applied
       before starting the sequence.
    """
    try:
        with cameras[camera_id].connect() as cam:
            if camera_id == 'cam2':
                if config:
                    status = cam.configure(config, quiet=quiet)
                    if status == SWIRCommandStatus.CameraNotInitialized:
                        log.error(log_name, f'Camera {camera_id} is not initialized')
                        return False

                    if status != SWIRCommandStatus.Succeeded:
                        log.error(log_name, f'Failed to configure camera {camera_id} with status {status}')
                        return False

                status = cam.start_sequence(count, quiet=quiet)
                if status == SWIRCommandStatus.Succeeded:
                    return True

                if status == SWIRCommandStatus.CameraNotInitialized:
                    log.error(log_name, f'Camera {camera_id} is not initialized')
                    return False
            else:
                if config:
                    status = cam.configure(config, quiet=quiet)
                    if status == QHYCommandStatus.CameraNotInitialized:
                        log.error(log_name, f'Camera {camera_id} is not initialized')
                        return False

                    if status != QHYCommandStatus.Succeeded:
                        log.error(log_name, f'Failed to configure camera {camera_id} with status {status}')
                        return False

                status = cam.start_sequence(count, quiet=quiet)
                if status == QHYCommandStatus.Succeeded:
                    return True

                if status == QHYCommandStatus.CameraNotInitialized:
                    log.error(log_name, f'Camera {camera_id} is not initialized')
                    return False

            log.error(log_name, f'Failed to start exposures on camera {camera_id} with status {status}')
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
    except Exception:
        log.error(log_name, 'Unknown error with camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
    return False


def cam_status(log_name, camera_id):
    """Returns the status dictionary for the camera"""
    try:
        with cameras[camera_id].connect() as camd:
            return camd.report_status()
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
    except Exception:
        log.error(log_name, 'Unknown error with camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
    return None


def cam_stop(log_name, camera_id, timeout=-1):
    """Aborts any active exposure sequences
       if timeout > 0 block for up to this many seconds for the
       camera to return to Idle (or Disabled) status before returning
    """
    try:
        if camera_id == 'cam2':
            with cameras[camera_id].connect() as camd:
                status = camd.stop_sequence()

            if status != SWIRCommandStatus.Succeeded:
                return False

            if timeout > 0:
                timeout_end = Time.now() + timeout * u.second
                while True:
                    with cameras[camera_id].connect() as camd:
                        data = camd.report_status()
                        if data.get('state', SWIRStatus.Idle) in [SWIRStatus.Idle, SWIRStatus.Disabled]:
                            return True

                    wait = min(1, (timeout_end - Time.now()).to(u.second).value)
                    if wait <= 0:
                        return False

                    time.sleep(wait)
        else:
            with cameras[camera_id].connect() as camd:
                status = camd.stop_sequence()

            if status != QHYCommandStatus.Succeeded:
                return False

            if timeout > 0:
                timeout_end = Time.now() + timeout * u.second
                while True:
                    with cameras[camera_id].connect() as camd:
                        data = camd.report_status()
                        if data.get('state', QHYStatus.Idle) in [QHYStatus.Idle, QHYStatus.Disabled]:
                            return True

                    wait = min(1, (timeout_end - Time.now()).to(u.second).value)
                    if wait <= 0:
                        return False

                    time.sleep(wait)
        return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
    except Exception:
        log.error(log_name, 'Unknown error while stopping camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
    return False


def cam_initialize(log_name, camera_id, timeout=20):
    """Initializes a given camera and resets configuration"""
    try:
        with cameras[camera_id].connect(timeout=timeout) as cam:
            if camera_id == 'cam2':
                status = cam.initialize()
                if status not in [SWIRCommandStatus.Succeeded,
                                  SWIRCommandStatus.CameraNotUninitialized]:
                    log.error(log_name, 'Failed to initialize camera ' + camera_id)
                    return False

                if cam.configure({}, quiet=True) != SWIRCommandStatus.Succeeded:
                    log.error(log_name, 'Failed to reset camera ' + camera_id + ' to defaults')
                    return False
            else:
                status = cam.initialize()
                if status not in [QHYCommandStatus.Succeeded,
                                  QHYCommandStatus.CameraNotUninitialized]:
                    log.error(log_name, 'Failed to initialize camera ' + camera_id)
                    return False

                if cam.configure({}, quiet=True) != QHYCommandStatus.Succeeded:
                    log.error(log_name, 'Failed to reset camera ' + camera_id + ' to defaults')
                    return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
    except Exception:
        log.error(log_name, 'Unknown error with camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
    return False


def cam_shutdown(log_name, camera_id):
    """Disables a given camera"""
    try:
        with cameras[camera_id].connect() as cam:
            cam.shutdown()
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
    except Exception:
        log.error(log_name, 'Unknown error with camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
    return False

# pylint: disable=unused-argument


def cam_is_active(log_name, camera_id, status):
    if camera_id == 'cam2':
        return status['state'] in [SWIRStatus.Acquiring, SWIRStatus.Reading]

    # cam1
    return status['state'] in [QHYStatus.Acquiring, QHYStatus.Reading]


def cam_is_idle(log_name, camera_id, status):
    if camera_id == 'cam2':
        return status['state'] == SWIRStatus.Idle

    # cam1
    return status['state'] == QHYStatus.Idle
# pylint: enable=unused-argument


def cam_is_warm(log_name, camera_id, status):
    if camera_id == 'cam2':
        if status['state'] == SWIRStatus.Disabled:
            return True

        if 'cooler_mode' not in status:
            log.error(log_name, 'Failed to check temperature on camera ' + camera_id)
            return True

        return status['cooler_mode'] == SWIRCoolerMode.Off

    # cam1
    if status['state'] == QHYStatus.Disabled:
        return True

    if 'cooler_mode' not in status:
        log.error(log_name, 'Failed to check temperature on camera ' + camera_id)
        return True

    return status['cooler_mode'] == QHYCoolerMode.Warm
