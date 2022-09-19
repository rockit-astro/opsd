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

"""Helper functions for actions to interact with the telescope mount"""

import sys
import traceback
import Pyro4
from warwick.observatory.common import daemons, log
from warwick.observatory.lmount import CommandStatus as TelCommandStatus

PARK_POSITION = 'stow'
PARK_TIMEOUT = 30
HOME_TIMEOUT = 60
SLEW_TIMEOUT = 60


def mount_init(log_name):
    """Initialize the mount"""
    try:
        with daemons.clasp_telescope.connect() as lmountd:
            return lmountd.initialize() in [TelCommandStatus.Succeeded, TelCommandStatus.MountNotDisabled]
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while initializing mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_home(log_name):
    """Homes the mount"""
    try:
        with daemons.clasp_telescope.connect(timeout=HOME_TIMEOUT) as lmountd:
            return lmountd.find_homes() == TelCommandStatus.Succeeded
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
        with daemons.clasp_telescope.connect() as lmountd:
            return lmountd.report_status()
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
        with daemons.clasp_telescope.connect(timeout=timeout) as lmountd:
            if tracking:
                status = lmountd.track_radec(ra, dec)
            else:
                status = lmountd.slew_radec(ra, dec)

            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew mount')
                return False
            return True
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
        with daemons.clasp_telescope.connect(timeout=timeout) as lmountd:
            status = lmountd.offset_radec(ra, dec)
            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to offset mount position')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while offsetting mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_slew_altaz(log_name, alt, az, tracking, timeout=SLEW_TIMEOUT):
    """Slew the mount to a given Alt, Az"""
    try:
        with daemons.clasp_telescope.connect(timeout=timeout) as lmountd:
            if tracking:
                status = lmountd.track_altaz(alt, az)
            else:
                status = lmountd.slew_altaz(alt, az)

            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew mount')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while slewing mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_slew_hadec(log_name, ha, dec, timeout=SLEW_TIMEOUT):
    """Slew the mount to a given HA, Dec"""
    try:
        with daemons.clasp_telescope.connect(timeout=timeout) as lmountd:
            status = lmountd.slew_hadec(ha, dec)
            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew mount')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while slewing mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_track_tle(log_name, tle, timeout=SLEW_TIMEOUT):
    """Slew the mount to track a given TLE"""
    try:
        with daemons.clasp_telescope.connect(timeout=timeout) as lmountd:
            status = lmountd.track_tle(tle[0], tle[1], tle[2])
            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew mount')
                return False
            return True
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
        with daemons.clasp_telescope.connect() as lmountd:
            lmountd.stop()
        return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while stopping mount')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_add_pointing_model_point(log_name, ra_j2000_deg, dec_j2000_deg):
    """Stop the mount tracking or movement"""
    try:
        with daemons.clasp_telescope.connect() as lmountd:
            lmountd.add_pointing_model_point(ra_j2000_deg, dec_j2000_deg)
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
        with daemons.clasp_telescope.connect(timeout=PARK_TIMEOUT) as lmountd:
            status = lmountd.park(PARK_POSITION)
            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to park mount')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with mount daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while parking mount')
        traceback.print_exc(file=sys.stdout)
        return False
