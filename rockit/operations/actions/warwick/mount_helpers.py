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

"""Helper functions for actions to interact with the telescope mount"""

import sys
import time
import traceback
import Pyro4
from rockit.common import daemons, log
from rockit.mount.meade import CommandStatus as TelCommandStatus
from rockit.ashdome import AzimuthStatus

PARK_POSITION = 'stow'
PARK_TIMEOUT = 120
SLEW_TIMEOUT = 120


def _wait_for_dome_azimuth(log_name):
    """Blocks while the dome azimuth status reports MOVING"""
    try:
        while True:
            with daemons.warwick_dome.connect() as domed:
                status = domed.status()

            if status.get('azimuth_status', None) != AzimuthStatus.Moving:
                return True

            time.sleep(5)
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while querying mount status')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_init(log_name):
    """Initialize the mount"""
    try:
        with daemons.warwick_telescope.connect() as teld:
            return teld.initialize() in [TelCommandStatus.Succeeded, TelCommandStatus.NotDisconnected]
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while initializing mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_status(log_name):
    """Returns the mount status dict or None on error"""
    try:
        with daemons.warwick_telescope.connect() as teld:
            return teld.report_status()
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return None
    except Exception:
        log.error(log_name, 'Unknown error while querying mount status')
        traceback.print_exc(file=sys.stdout)
        return None


def mount_slew_radec(log_name, ra, dec, tracking, timeout=SLEW_TIMEOUT):
    """Slew the mount to a given RA, Dec"""
    try:
        with daemons.warwick_telescope.connect(timeout=timeout) as teld:
            if tracking:
                status = teld.track_radec(ra, dec)
            else:
                status = teld.slew_radec(ra, dec)

            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew mount')
                return False

            return _wait_for_dome_azimuth(log_name)
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while slewing mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_offset_radec(log_name, ra, dec, timeout=SLEW_TIMEOUT):
    """Offset the mount by a given RA, Dec"""
    try:
        with daemons.warwick_telescope.connect(timeout=timeout) as teld:
            status = teld.offset_radec(ra, dec)
            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to offset mount position')
                return False

            return _wait_for_dome_azimuth(log_name)
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while offsetting mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_slew_altaz(log_name, alt, az, timeout=SLEW_TIMEOUT):
    """Slew the mount to a given Alt, Az"""
    try:
        with daemons.warwick_telescope.connect(timeout=timeout) as teld:
            status = teld.slew_altaz(alt, az)

            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew mount')
                return False

            return _wait_for_dome_azimuth(log_name)
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while slewing mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_stop(log_name):
    """Stop the mount tracking or movement"""
    try:
        with daemons.warwick_telescope.connect() as teld:
            teld.stop()
        return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while stopping mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_park(log_name):
    """Park the telescope in the stow position"""
    try:
        with daemons.warwick_telescope.connect(timeout=PARK_TIMEOUT) as teld:
            status = teld.park(PARK_POSITION)
            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to park mount')
                return False

            return _wait_for_dome_azimuth(log_name)
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while parking mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_sync(log_name, ra, dec):
    """Sync the mount position to the given RA, Dec"""
    try:
        with daemons.warwick_telescope.connect() as teld:
            status = teld.sync(ra, dec)
            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to sync mount position')
                return False

            return _wait_for_dome_azimuth(log_name)
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while syncing mount')
        traceback.print_exc(file=sys.stdout)
        return False
