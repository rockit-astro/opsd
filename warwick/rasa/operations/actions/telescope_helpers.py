#!/usr/bin/env python3
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

"""Helper functions for actions to interact with the telescope mount and focuser"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import sys
import traceback
import Pyro4
from warwick.observatory.common import (
    daemons,
    log)
from warwick.rasa.focuser import CommandStatus as FocCommandStatus
from warwick.rasa.telescope import CommandStatus as TelCommandStatus

def tel_slew_radec(log_name, ra, dec, tracking, timeout):
    """Slew the telescope to a given RA, Dec"""
    try:
        with daemons.rasa_telescope.connect(timeout=timeout) as teld:
            if tracking:
                status = teld.track_radec(ra, dec)
            else:
                status = teld.slew_radec(ra, dec)

            if status != TelCommandStatus.Succeeded:
                print('Failed to slew telescope')
                log.error(log_name, 'Failed to slew telescope')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with telescope daemon')
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        print('Unknown error while slewing telescope')
        traceback.print_exc(file=sys.stdout)
        log.error(log_name, 'Unknown error while slewing telescope')
        return False

def tel_offset_radec(log_name, ra, dec, timeout):
    """Offset the telescope by a given RA, Dec"""
    try:
        with daemons.rasa_telescope.connect(timeout=timeout) as teld:
            status = teld.offset_radec(ra, dec)
            if status != TelCommandStatus.Succeeded:
                print('Failed to offset telescope position')
                log.error(log_name, 'Failed to offset telescope position')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with telescope daemon')
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        print('Unknown error while offsetting telescope')
        traceback.print_exc(file=sys.stdout)
        log.error(log_name, 'Unknown error while offsetting telescope')
        return False

def tel_stop(log_name):
    """Stop the telescope tracking or movement"""
    try:
        with daemons.rasa_telescope.connect() as teld:
            teld.stop()
        return True
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with telescope daemon')
        log.error(log_name, 'Failed to communicate with telescope daemon')
        return False
    except Exception:
        print('Unknown error while stopping telescope')
        traceback.print_exc(file=sys.stdout)
        log.error(log_name, 'Unknown error while stopping telescope')
        return False

def set_focus(log_name, channel, position, timeout):
    """Set the given focuser channel to the given position"""
    try:
        with daemons.rasa_focus.connect(timeout=timeout) as focusd:
            print('moving focus {} to {}'.format(channel, position))
            status = focusd.set_focus(channel, position)
            if status != FocCommandStatus.Succeeded:
                print('Failed to set focuser position')
                log.error(log_name, 'Failed to set focuser position')
                return False
            print('done')
            return True
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with focuser daemon')
        log.error(log_name, 'Failed to communicate with focuser daemon')
        return False
    except Exception as e:
        print('Unknown error while configuring focuser')
        print(e)
        log.error(log_name, 'Unknown error while configuring focuser')
        return False