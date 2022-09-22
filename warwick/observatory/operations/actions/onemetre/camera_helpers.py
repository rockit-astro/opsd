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

import sys
import time
import traceback
from astropy.time import Time
import astropy.units as u
import Pyro4
from warwick.observatory.common import daemons, log
from warwick.observatory.camera.andor import CameraStatus, CommandStatus as CamCommandStatus

cameras = {
    'blue': daemons.onemetre_blue_camera,
    'red': daemons.onemetre_red_camera
}


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

            if not config or status == CamCommandStatus.Succeeded:
                status = cam.start_sequence(count, quiet=quiet)

            if status != CamCommandStatus.Succeeded:
                log.error(log_name, 'Failed to start exposure sequence on camera ' + camera_id)
                return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
        return False
    except Exception:
        log.error(log_name, 'Unknown error with camera ' + camera_id)
        traceback.print_exc(file=sys.stdout)
        return False


def cam_status(log_name, camera_id):
    """Returns the camera status dict or None on error"""
    try:
        with cameras[camera_id].connect() as camd:
            return camd.report_status()
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera ' + camera_id)
        return None
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
        with cameras[camera_id].connect() as camd:
            status = camd.stop_sequence()

        if status != CamCommandStatus.Succeeded:
            return False

        if timeout > 0:
            timeout_end = Time.now() + timeout * u.second
            while True:
                with cameras[camera_id].connect() as camd:
                    data = camd.report_status()
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
