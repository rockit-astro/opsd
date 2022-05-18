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

"""Helper functions for actions to interact with the focuser"""

import sys
import traceback
import Pyro4
from warwick.observatory.common import daemons, log
from warwick.observatory.focuslynx import FocuserStatus, CommandStatus as FocCommandStatus

channels = {
    'cam1': 1,
    'cam2': 2,
}


def focus_get(log_name, camera_id):
    """Returns the requested focuser position or None on error
       Requires focuser to be idle
    """
    try:
        with daemons.clasp_focus.connect() as focusd:
            channel = channels[camera_id]
            status = focusd.report_status()
            if status['status_' + str(channel)] != FocuserStatus.Idle:
                print('Focuser status is not idle')
                log.error(log_name, 'Focuser status is not idle')
                return None
            return status['current_steps_' + str(channel)]
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with focuser daemon')
        log.error(log_name, 'Failed to communicate with focuser daemon')
        return None
    except Exception:
        print('Unknown error while querying focuser position')
        traceback.print_exc(file=sys.stdout)
        log.error(log_name, 'Unknown error while querying focuser position')
        return None


def focus_set(log_name, camera_id, position, timeout):
    """Set the given focuser channel to the given position"""
    try:
        with daemons.clasp_focus.connect(timeout=timeout) as focusd:
            channel = channels[camera_id]
            print('moving focus {} to {}'.format(channel, position))
            status = focusd.set_focus(channel, position)
            if status != FocCommandStatus.Succeeded:
                print('Failed to set focuser position')
                log.error(log_name, 'Failed to set focuser position')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with focuser daemon')
        log.error(log_name, 'Failed to communicate with focuser daemon')
        return False
    except Exception:
        print('Unknown error while configuring focuser')
        traceback.print_exc(file=sys.stdout)
        log.error(log_name, 'Unknown error while configuring focuser')
        return False


def focus_stop(log_name, camera_id):
    """Stop the focuser movement"""
    try:
        with daemons.clasp_focus.connect() as focusd:
            focusd.stop_channel(channels[camera_id])
        return True
    except Pyro4.errors.CommunicationError:
        print('Failed to communicate with focuser daemon')
        log.error(log_name, 'Failed to communicate with focuser daemon')
        return False
    except Exception:
        print('Unknown error while stopping focuser')
        traceback.print_exc(file=sys.stdout)
        log.error(log_name, 'Unknown error while stopping focuser')
        return False
