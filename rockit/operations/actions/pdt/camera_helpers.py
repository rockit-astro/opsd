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

"""Helper functions for actions to interact with the cameras"""

# pylint: disable=too-many-return-statements

import sys
import threading
import time
import traceback
from astropy.time import Time
import astropy.units as u
import Pyro4
from rockit.camera.qhy import CameraStatus, CommandStatus as CamCommandStatus
from rockit.common import daemons, log

cameras = {
    'cam1': daemons.pdt_cam1,
    'cam2': daemons.pdt_cam2,
}

CAMERA_POWERON_DELAY = 5
CAMERA_INIT_TIMEOUT = 60

def cam_initialize(log_name, camera_id, timeout=CAMERA_INIT_TIMEOUT):
    """Initializes a given camera and resets configuration"""
    try:
        with cameras[camera_id].connect(timeout=timeout) as cam:
            status = cam.initialize()
            if status not in [CamCommandStatus.Succeeded,
                              CamCommandStatus.CameraNotUninitialized]:
                log.error(log_name, 'Failed to initialize camera ' + camera_id)
                return False

            if cam.configure({}, quiet=True) != CamCommandStatus.Succeeded:
                log.error(log_name, 'Failed to reset camera ' + camera_id + ' to defaults')
                return False
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
        return False
    except Exception:
        log.error(log_name, 'Unknown error with camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
        return False
    return True


def cam_configure(log_name, camera_id, config=None, quiet=False):
    """Set camera configuration
       config is assumed to contain a dictionary of camera
       configuration that has been validated by the camera schema.
    """
    try:
        with cameras[camera_id].connect() as cam:
            if config:
                status = cam.configure(config, quiet=quiet)

            if status == CamCommandStatus.Succeeded:
                return True

            if status == CamCommandStatus.CameraNotInitialized:
                log.error(log_name, f'Camera {camera_id} is not initialized')
                return False

            log.error(log_name, f'Failed to configure camera {camera_id} with status {status}')
            return False
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
        return False
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
            if config:
                status = cam.configure(config, quiet=quiet)
                if status == CamCommandStatus.CameraNotInitialized:
                    log.error(log_name, f'Camera {camera_id} is not initialized')
                    return False

                if status != CamCommandStatus.Succeeded:
                    log.error(log_name, f'Failed to configure camera {camera_id} with status {status}')
                    return False

            status = cam.start_sequence(count, quiet=quiet)
            if status == CamCommandStatus.Succeeded:
                return True

            if status == CamCommandStatus.CameraNotInitialized:
                log.error(log_name, f'Camera {camera_id} is not initialized')
                return False

            log.error(log_name, f'Failed to start exposures on camera {camera_id} with status {status}')
            return False
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
        return False
    except Exception:
        log.error(log_name, 'Unknown error with camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
        return False


def cam_status(log_name, camera_id):
    """Returns the status dictionary for the camera"""
    try:
        with cameras[camera_id].connect() as camd:
            return camd.report_status() or {}
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
        return {}
    except Exception:
        log.error(log_name, 'Unknown error with camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
        return {}


def cam_stop(log_name, camera_id, timeout=-1):
    """Aborts any active exposure sequences
       if timeout > 0 block for up to this many seconds for the
       camera to return to Idle (or Disabled) status before returning
    """
    try:
        with cameras[camera_id].connect() as camd:
            status = camd.stop_sequence()

        if status != CamCommandStatus.Succeeded:
            return False

        if timeout > 0:
            timeout_end = Time.now() + timeout * u.second
            while True:
                with cameras[camera_id].connect() as camd:
                    data = camd.report_status() or {}
                    if data.get('state', CameraStatus.Idle) in [CameraStatus.Idle, CameraStatus.Disabled]:
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
