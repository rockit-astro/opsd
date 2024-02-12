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
from astropy.time import Time
import astropy.units as u
from rockit.common import daemons, log
from rockit.mount.talon import CommandStatus as TelCommandStatus
from rockit.covers import CommandStatus as CoversCommandStatus, CoversState

PARK_ALTAZ = (45, 45)
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


def _move_covers(log_name, state):
    try:
        with daemons.onemetre_covers.connect() as coversd:
            if state == CoversState.Open:
                return coversd.open_covers(blocking=False) == CoversCommandStatus.Succeeded
            if state == CoversState.Closed:
                return coversd.close_covers(blocking=False) == CoversCommandStatus.Succeeded
            return False
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with covers daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while moving covers')
        traceback.print_exc(file=sys.stdout)
        return False


def _wait_for_covers(log_name, desired_state, timeout=30):
    moving_state = CoversState.Opening if desired_state == CoversState.Open else CoversState.Closing
    time.sleep(2)
    start_time = Time.now()
    try:
        while True:
            time.sleep(1)
            if Time.now() - start_time > timeout * u.s:
                return False

            with daemons.onemetre_covers.connect() as coversd:
                state = (coversd.report_status() or {}).get('state', CoversState.Disabled)
                if state == desired_state:
                    return True
                if state != moving_state:
                    return False
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with covers daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while moving covers')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_slew_radec(log_name, ra, dec, tracking, open_covers=False, timeout=SLEW_TIMEOUT):
    try:
        if open_covers:
            _move_covers(log_name, CoversState.Open)

        with daemons.onemetre_telescope.connect(timeout=timeout) as teld:
            if tracking:
                status = teld.track_radec(ra, dec)
            else:
                status = teld.slew_radec(ra, dec)

            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew telescope')
                return False

        return not open_covers or _wait_for_covers(log_name, CoversState.Open)
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


def mount_slew_altaz(log_name, alt, az, tracking, open_covers=False, timeout=SLEW_TIMEOUT):
    """Slew the telescope to a given Alt, Az"""
    try:
        if open_covers:
            _move_covers(log_name, CoversState.Open)

        with daemons.onemetre_telescope.connect(timeout=timeout) as teld:
            if tracking:
                status = teld.track_altaz(alt, az)
            else:
                status = teld.slew_altaz(alt, az)

            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew telescope')
                return False

        return not open_covers or _wait_for_covers(log_name, CoversState.Open)
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while slewing telescope')
        traceback.print_exc(file=sys.stdout)
        return False


def mount_slew_hadec(log_name, ha, dec, open_covers=False, timeout=SLEW_TIMEOUT):
    """Slew the telescope to a given HA, Dec"""
    try:
        if open_covers:
            _move_covers(log_name, CoversState.Open)

        with daemons.onemetre_telescope.connect(timeout=timeout) as teld:
            status = teld.slew_hadec(ha, dec)
            if status != TelCommandStatus.Succeeded:
                log.error(log_name, 'Failed to slew telescope')
                return False

        return not open_covers or _wait_for_covers(log_name, CoversState.Open)
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


def mount_park(log_name, close_covers=False):
    """Park the telescope pointing at zenith"""
    if close_covers:
        _move_covers(log_name, CoversState.Closed)

    if not mount_slew_altaz(log_name, PARK_ALTAZ[0], PARK_ALTAZ[1], False, timeout=PARK_TIMEOUT) :
        return False

    return not close_covers or _wait_for_covers(log_name, CoversState.Closed)
