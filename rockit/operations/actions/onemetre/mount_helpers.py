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
import traceback
import Pyro4
from rockit.common import daemons, log
from rockit.mount.talon import CommandStatus as TelCommandStatus

PARK_ALTAZ = (35, 25)
PARK_TIMEOUT = 60
INIT_TIMEOUT = 30
HOME_TIMEOUT = 300
SLEW_TIMEOUT = 60

def mount_status(log_name):
    """Returns the telescope status dict or None on error"""
    try:
        with daemons.onemetre_telescope.connect() as teld:
            return teld.report_status()
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return None
    except Exception:
        log.error(log_name, 'Unknown error while querying telescope status')
        traceback.print_exc(file=sys.stdout)
        return None


def mount_init(log_name):
    """Initialize the telescope"""
    try:
        with daemons.onemetre_telescope.connect(timeout=INIT_TIMEOUT) as teld:
            return teld.initialize() in [TelCommandStatus.Succeeded, TelCommandStatus.TelescopeNotUninitialized]
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while initializing telescope')
        traceback.print_exc(file=sys.stdout)
        return False

def tel_home(log_name):
    """Homes the telescope"""
    try:
        with daemons.onemetre_telescope.connect(timeout=HOME_TIMEOUT) as teld:
            return teld.find_homes() == TelCommandStatus.Succeeded
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while initializing telescope')
        traceback.print_exc(file=sys.stdout)
        return False

def mount_slew_radec(log_name, ra, dec, tracking, timeout=SLEW_TIMEOUT):
    """Slew the telescope to a given RA, Dec"""
    try:
        with daemons.onemetre_telescope.connect(timeout=timeout) as teld:
            if tracking:
                status = teld.track_radec(ra, dec)
            else:
                status = teld.slew_radec(ra, dec)

            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew telescope')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while slewing telescope')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_offset_radec(log_name, ra, dec, timeout=SLEW_TIMEOUT):
    """Offset the telescope by a given RA, Dec"""
    try:
        with daemons.onemetre_telescope.connect(timeout=timeout) as teld:
            status = teld.offset_radec(ra, dec)
            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to offset telescope position')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while offsetting telescope')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_slew_altaz(log_name, alt, az, tracking, timeout=SLEW_TIMEOUT):
    """Slew the telescope to a given Alt, Az"""
    try:
        with daemons.onemetre_telescope.connect(timeout=timeout) as teld:
            if tracking:
                status = teld.track_altaz(alt, az)
            else:
                status = teld.slew_altaz(alt, az)

            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew telescope')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while slewing telescope')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_slew_hadec(log_name, ha, dec, timeout=SLEW_TIMEOUT):
    """Slew the telescope to a given HA, Dec"""
    try:
        with daemons.onemetre_telescope.connect(timeout=timeout) as teld:
            status = teld.slew_hadec(ha, dec)
            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew telescope')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while slewing telescope')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_stop(log_name):
    """Stop the telescope tracking or movement"""
    try:
        with daemons.onemetre_telescope.connect() as teld:
            teld.stop()
        return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while stopping telescope')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_park(log_name):
    """Park the telescope pointing at zenith"""
    return mount_slew_altaz(log_name, PARK_ALTAZ[0], PARK_ALTAZ[1], False, PARK_TIMEOUT)
