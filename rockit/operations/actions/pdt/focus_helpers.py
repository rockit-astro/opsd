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

"""Helper functions for actions to interact with the focuser"""

import sys
import traceback
import Pyro4
from rockit.common import daemons, log
from rockit.focuser.klipper import FocuserStatus, CommandStatus as FocCommandStatus
from rockit.klippermcu import StepperStatus

FOCUS_TIMEOUT=300


def focus_get(log_name, camera_id):
    """Returns the requested focuser position or None on error
       Requires focuser to be idle
    """
    try:
        with daemons.pdt_focuser.connect() as focusd:
            status = focusd.report_status()
            if status['status'] != FocuserStatus.Connected:
                log.error(log_name, 'Focuser is offline')
                return None

            channel = status.get('channels', {}).get(camera_id)
            if channel['status'] != StepperStatus.NotHomed:
                log.error(log_name, 'Focuser is not homed')
                return None
            if channel['status'] != StepperStatus.Idle:
                log.error(log_name, 'Focuser is moving')
                return None
            return channel['pos']
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with focuser daemon')
        return None
    except Exception:
        traceback.print_exc(file=sys.stdout)
        log.error(log_name, 'Unknown error while querying focuser position')
        return None


def focus_set(log_name, camera_id, position, timeout=FOCUS_TIMEOUT):
    """Set the given focuser channel to the given position"""
    try:
        with daemons.pdt_focuser.connect(timeout=timeout) as focusd:
            print(f'moving focus {camera_id} to {position}')
            status = focusd.set_channel(camera_id, position)
            if status != FocCommandStatus.Succeeded:
                log.error(log_name, 'Failed to set focuser position')
                return False
            return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with focuser daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while configuring focuser')
        traceback.print_exc(file=sys.stdout)
        return False


def focus_stop(log_name, camera_id):
    """Stop the focuser movement"""
    try:
        with daemons.pdt_focuser.connect() as focusd:
            focusd.stop(camera_id)
        return True
    except Pyro4.errors.CommunicationError:
        log.error(log_name, 'Failed to communicate with focuser daemon')
        return False
    except Exception:
        log.error(log_name, 'Unknown error while stopping focuser')
        traceback.print_exc(file=sys.stdout)
        return False
