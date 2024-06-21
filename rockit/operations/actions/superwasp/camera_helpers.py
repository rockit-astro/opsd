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
    'cam1': daemons.superwasp_cam1,
    'cam2': daemons.superwasp_cam2,
    'cam3': daemons.superwasp_cam3,
    'cam4': daemons.superwasp_cam4,
}

das_machines = {
    'das1': {'daemon': daemons.superwasp_camvirt_das1, 'cameras': ['cam1', 'cam2']},
    'das2': {'daemon': daemons.superwasp_camvirt_das2, 'cameras': ['cam3', 'cam4']},
}

CAMERA_POWERON_DELAY = 5


def _cam_run_synchronised(log_name, camera_ids, func, timeout=5):
    """Run a function simultaneously on multiple cameras"""
    threads = []
    success = []
    sync_condition = threading.Condition()

    def run(camera_id):
        ret = False
        with sync_condition:
            sync_condition.wait()
        try:
            with cameras[camera_id].connect(timeout=timeout) as cam:
                status = func(cam)
            if status == CamCommandStatus.Succeeded:
                ret = True

        except Pyro4.errors.CommunicationError:
            log.error(log_name, 'Failed to communicate with camera ' + camera_id)
        success.append(ret)

    for camera_id in camera_ids:
        thread = threading.Thread(target=run, args=(camera_id,))
        thread.start()
        threads.append(thread)

    time.sleep(1)
    with sync_condition:
        sync_condition.notify_all()

    for thread in threads:
        thread.join()

    return all(success)


def cam_reinitialize_synchronised(log_name, camera_ids, attempts=1):
    """Initialize multiple cameras simultaneously to
       synchronise their internal polling loops
    """
    for _ in range(attempts):
        for camera_id in camera_ids:
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

        time.sleep(5)

        # Initialisation may sporadically take longer, so wait and retry if needed
        if _cam_run_synchronised(log_name, camera_ids, lambda c: c.initialize(), timeout=30):
            return True

        time.sleep(30)
    return False


def cam_start_synchronised(log_name, camera_ids, count=0, quiet=False):
    """Start multiple cameras exposing simultaneously"""
    return _cam_run_synchronised(log_name, camera_ids, lambda c: c.start_sequence(count, quiet=quiet))


def cam_stop_synchronised(log_name, camera_ids, timeout=0, quiet=False):
    """Aborts multiple cameras simultaneously
       if timeout > 0 block for up to this many seconds for the
       camera to return to Idle (or Disabled) status before returning
    """
    success = _cam_run_synchronised(log_name, camera_ids, lambda c: c.stop_sequence(quiet=quiet))
    if not success:
        return False

    if timeout > 0:
        timeout_end = Time.now() + timeout * u.second
        complete = {camera_id: False for camera_id in camera_ids}
        while True:
            for camera_id in camera_ids:
                if not complete[camera_id]:
                    try:
                        with cameras[camera_id].connect() as camd:
                            data = camd.report_status() or {}
                            complete[camera_id] = data.get('state', CameraStatus.Idle) in \
                                [CameraStatus.Idle, CameraStatus.Disabled]
                    except Pyro4.errors.CommunicationError:
                        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
                        return False
                    except Exception:
                        log.error(log_name, 'Unknown error with camera ' + camera_id)
                        traceback.print_exc(file=sys.stdout)
                        return False

            if all(complete.values()):
                return True

            wait = min(1, (timeout_end - Time.now()).to(u.second).value)
            if wait <= 0:
                return False

            time.sleep(wait)
    return True


def cam_initialize(log_name, camera_id, timeout=20):
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


def cam_switch_power(log_name, camera_ids, enabled):
    switched = False
    try:
        with daemons.superwasp_power.connect() as powerd:
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
        with daemons.superwasp_power.connect() as power:
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


def cam_initialize_vms(log_name, das_ids):
    def boot_vms(daemon):
        try:
            with daemon.connect(timeout=100) as camvirtd:
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


def cam_shutdown_vms(log_name, das_ids):
    def shutdown_vms(daemon):
        try:
            with daemon.connect(timeout=40) as camvirtd:
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
