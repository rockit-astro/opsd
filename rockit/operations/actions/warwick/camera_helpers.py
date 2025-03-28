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
import time
import traceback
from astropy.time import Time
import astropy.units as u
import Pyro4
from rockit.cfw import CommandStatus as CFWCommandStatus
from rockit.camera.qhy import CameraStatus, CommandStatus as CamCommandStatus
from rockit.common import daemons, log
from .focus_helpers import focus_offset

filters = ['I', 'R', 'V', 'B', 'NONE']

# Defined relative to NONE
FOCUS_OFFSETS = {
    'NONE': 0,
    'B': 18135,
    'V': 19069,
    'R': 18511,
    'I': 19406,
    'BLOCK': 0
}

FILTER_TIMEOUT = 30


def cam_set_filter(log_name, filter_name, timeout=FILTER_TIMEOUT):
    if filter_name not in FOCUS_OFFSETS:
        log.error(log_name, f'Unknown filter {filter_name}')
        return False

    try:
        with daemons.warwick_filterwheel.connect(timeout=timeout) as cfwd:
            current_filter = cfwd.report_status().get('filter', None)
            if current_filter not in FOCUS_OFFSETS:
                log.error(log_name, f'Unknown filter {current_filter}')
                return False

            if filter_name == current_filter:
                return True

            status = cfwd.set_filter(filter_name)
            if status != CFWCommandStatus.Succeeded:
                log.error(log_name, f'Failed to configure filter wheel with status {status}')
                return False
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with filter wheel')
        return False
    except Exception:
        log.error(log_name, 'Unknown error with filter wheel')
        traceback.print_exc(file=sys.stdout)
        return False

    return focus_offset(log_name, FOCUS_OFFSETS[filter_name] - FOCUS_OFFSETS[current_filter])


def cam_configure(log_name, config=None, quiet=False):
    """Set camera configuration
       config is assumed to contain a dictionary of camera
       configuration that has been validated by the camera schema.
    """
    config = config or {}
    if not cam_set_filter(log_name, config.pop('filter', 'NONE')):
        return False

    try:
        with daemons.warwick_camera.connect() as cam:
            status = cam.configure(config, quiet=quiet)

            if status == CamCommandStatus.Succeeded:
                return True

            if status == CamCommandStatus.CameraNotInitialized:
                log.error(log_name, 'Camera is not initialized')
                return False

            log.error(log_name, f'Failed to configure camera with status {status}')
            return False
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera')
        return False
    except Exception:
        log.error(log_name, 'Unknown error with camera')
        traceback.print_exc(file=sys.stdout)
        return False


def cam_take_images(log_name, count=1, config=None, quiet=False):
    """Start an exposure sequence with count images

       If config is non-None it is assumed to contain
       a dictionary of camera configuration that has been
       validated by the camera schema, which is applied
       before starting the sequence.
    """
    if config:
        config = config.copy()
        if not cam_set_filter(log_name, config.pop('filter', 'NONE')):
            return False

    try:
        with daemons.warwick_camera.connect() as cam:
            if config:
                status = cam.configure(config, quiet=quiet)
                if status == CamCommandStatus.CameraNotInitialized:
                    log.error(log_name, 'Camera is not initialized')
                    return False

                if status != CamCommandStatus.Succeeded:
                    log.error(log_name, f'Failed to configure camera with status {status}')
                    return False

            status = cam.start_sequence(count, quiet=quiet)
            if status == CamCommandStatus.Succeeded:
                return True

            if status == CamCommandStatus.CameraNotInitialized:
                log.error(log_name, 'Camera is not initialized')
                return False

            log.error(log_name, f'Failed to start exposures with status {status}')
            return False
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera')
        return False
    except Exception:
        log.error(log_name, 'Unknown error with camera')
        traceback.print_exc(file=sys.stdout)
        return False


def cam_status(log_name):
    """Returns the status dictionary for the camera"""
    try:
        with daemons.warwick_camera.connect() as camd:
            return camd.report_status() or {}
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera')
        return {}
    except Exception:
        log.error(log_name, 'Unknown error with camera')
        traceback.print_exc(file=sys.stdout)
        return {}


def cam_stop(log_name, timeout=-1):
    """Aborts any active exposure sequences
       if timeout > 0 block for up to this many seconds for the
       camera to return to Idle (or Disabled) status before returning
    """
    try:
        with daemons.warwick_camera.connect() as camd:
            status = camd.stop_sequence()

        if status != CamCommandStatus.Succeeded:
            return False

        if timeout > 0:
            timeout_end = Time.now() + timeout * u.second
            while True:
                with daemons.warwick_camera.connect() as camd:
                    data = camd.report_status() or {}
                    if data.get('state', CameraStatus.Idle) in [CameraStatus.Idle, CameraStatus.Disabled]:
                        return True

                wait = min(1, (timeout_end - Time.now()).to(u.second).value)
                if wait <= 0:
                    return False

                time.sleep(wait)
        return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with camera')
    except Exception:
        log.error(log_name, 'Unknown error while stopping camera')
        traceback.print_exc(file=sys.stdout)
    return False
