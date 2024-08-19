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
from rockit.camera.qhy import (
    CameraStatus as QHYStatus,
    CommandStatus as QHYCommandStatus,
    CoolerMode as QHYCoolerMode)
from rockit.camera.raptor import (
    CameraStatus as SWIRStatus,
    CommandStatus as SWIRCommandStatus,
    CoolerMode as SWIRCoolerMode)
from rockit.common import daemons, log

cameras = {
    'cam1': daemons.clasp_camera_1,
    'cam2': daemons.clasp_camera_2,
}

das_machines = {
    'das1': {'daemon': daemons.clasp_camvirt_1, 'cameras': ['cam1']},
    'das2': {'daemon': daemons.clasp_camvirt_2, 'cameras': ['cam2']},
}

CAMERA_POWERON_DELAY = 5
CAMERA_INIT_TIMEOUT = 60
CAMERA_VM_TIMEOUT = 300

COMMAND_SUCCESS = {
    'cam1': QHYCommandStatus.Succeeded,
    'cam2': SWIRCommandStatus.Succeeded
}

COMMAND_NOT_INITIALIZED = {
    'cam1': QHYCommandStatus.CameraNotInitialized,
    'cam2': SWIRCommandStatus.CameraNotInitialized
}

COMMAND_NOT_UNINITIALIZED = {
    'cam1': QHYCommandStatus.CameraNotUninitialized,
    'cam2': SWIRCommandStatus.CameraNotUninitialized
}

STATUS_DISABLED = {
    'cam1': QHYStatus.Disabled,
    'cam2': SWIRStatus.Disabled
}

STATUS_IDLE = {
    'cam1': QHYStatus.Idle,
    'cam2': SWIRStatus.Idle
}

STATUS_ACQUIRING = {
    'cam1': QHYStatus.Acquiring,
    'cam2': SWIRStatus.Acquiring
}

STATUS_READING = {
    'cam1': QHYStatus.Reading,
    'cam2': SWIRStatus.Reading
}

COOLER_WARM = {
    'cam1': QHYCoolerMode.Warm,
    'cam2': SWIRCoolerMode.Off
}


def cam_configure(log_name, camera_id, config, quiet=False):
    """Set camera configuration
       config is assumed to contain a dictionary of camera
       configuration that has been validated by the camera schema.
    """

    try:
        with cameras[camera_id].connect() as cam:
            status = cam.configure(config, quiet=quiet)
            if status == COMMAND_SUCCESS[camera_id]:
                return True

            if status == COMMAND_NOT_INITIALIZED[camera_id]:
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
            if config:
                status = cam.configure(config, quiet=quiet)
                if status == COMMAND_NOT_INITIALIZED[camera_id]:
                    log.error(log_name, f'Camera {camera_id} is not initialized')
                    return False

                if status != COMMAND_SUCCESS[camera_id]:
                    log.error(log_name, f'Failed to configure camera {camera_id} with status {status}')
                    return False

            status = cam.start_sequence(count, quiet=quiet)
            if status == COMMAND_SUCCESS[camera_id]:
                return True

            if status == COMMAND_NOT_INITIALIZED[camera_id]:
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
            return camd.report_status() or {}
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
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

        if status != COMMAND_SUCCESS[camera_id]:
            return False

        if timeout > 0:
            timeout_end = Time.now() + timeout * u.second
            while True:
                with cameras[camera_id].connect() as camd:
                    data = camd.report_status() or {}
                    if data.get('state', STATUS_IDLE[camera_id]) in \
                            [STATUS_IDLE[camera_id], STATUS_DISABLED[camera_id]]:
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


def cam_initialize(log_name, camera_id, timeout=CAMERA_INIT_TIMEOUT):
    """Initializes a given camera and resets configuration"""
    try:
        with cameras[camera_id].connect(timeout=timeout) as cam:
            status = cam.initialize()
            if status not in [COMMAND_SUCCESS[camera_id], COMMAND_NOT_UNINITIALIZED[camera_id]]:
                log.error(log_name, 'Failed to initialize camera ' + camera_id)
                return False

            if cam.configure({}, quiet=True) != COMMAND_SUCCESS[camera_id]:
                log.error(log_name, f'Failed to reset camera {camera_id} to defaults')
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
    return status.get('state', STATUS_ACQUIRING[camera_id]) in [STATUS_ACQUIRING[camera_id], STATUS_READING[camera_id]]


def cam_is_idle(log_name, camera_id, status):
    return status.get('state', STATUS_IDLE[camera_id]) == STATUS_IDLE[camera_id]

# pylint: enable=unused-argument


def cam_is_warm(log_name, camera_id, status):
    if 'state' not in status or 'cooler_mode' not in status:
        log.error(log_name, 'Failed to check temperature on camera ' + camera_id)
        return True

    if status['state'] == STATUS_DISABLED[camera_id]:
        return True

    return status['cooler_mode'] == COOLER_WARM[camera_id]


def cam_switch_power(log_name, camera_ids, enabled):
    switched = False
    try:
        with daemons.clasp_power.connect() as powerd:
            p = powerd.last_measurement()
            for camera_id in camera_ids:
                if camera_id in p and p[camera_id] != enabled:
                    switched = True
                    powerd.switch(camera_id, enabled)
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with power daemon')
    except Exception:
        log.error(log_name, 'Unknown error with power daemon')
        traceback.print_exc(file=sys.stdout)

    if enabled and switched:
        # Wait for cameras to power up
        time.sleep(CAMERA_POWERON_DELAY)


def cam_cycle_power(log_name, camera_id):
    try:
        with cameras[camera_id].connect() as cam:
            cam.shutdown()
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
        return False
    except Exception:
        log.error(log_name, 'Unknown error with camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
        return False

    try:
        with daemons.clasp_power.connect() as power:
            power.switch(camera_id, False)
            time.sleep(10)
            power.switch(camera_id, True)
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with power daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error with power daemon')
        traceback.print_exc(file=sys.stdout)
        return False

    return True


def cam_initialize_vms(log_name, das_ids, timeout=CAMERA_VM_TIMEOUT):
    def boot_vms(daemon):
        try:
            with daemon.connect(timeout=timeout) as camvirtd:
                camvirtd.initialize()
        except Pyro4.errors.CommunicationError:
            log.error(log_name, 'Failed to communicate with camvirt daemon')
        except Exception:
            log.error(log_name, 'Unknown error with camvirt daemon')
            traceback.print_exc(file=sys.stdout)

    threads = []
    for das_id in das_ids:
        thread = threading.Thread(target=boot_vms, args=(das_machines[das_id]['daemon'],))
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()


def cam_shutdown_vms(log_name, das_ids, timeout=CAMERA_VM_TIMEOUT):
    def shutdown_vms(daemon):
        try:
            with daemon.connect(timeout=timeout) as camvirtd:
                camvirtd.shutdown()
        except Pyro4.errors.CommunicationError:
            log.error(log_name, 'Failed to communicate with camvirt daemon')
        except Exception:
            log.error(log_name, 'Unknown error with camvirt daemon')
            traceback.print_exc(file=sys.stdout)

    threads = []
    for das_id in das_ids:
        thread = threading.Thread(target=shutdown_vms, args=(das_machines[das_id]['daemon'],))
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()
