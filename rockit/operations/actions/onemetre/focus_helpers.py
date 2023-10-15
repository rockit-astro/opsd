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
from rockit.talon import CommandStatus as TelCommandStatus, FocusState

FOCUS_TIMEOUT = 300


def focus_get(log_name, camera_id):
    """Returns the requested focuser position or None on error
       Requires focuser to be Ready
    """
    if camera_id == 'blue':
        try:
            with daemons.onemetre_telescope.connect() as teld:
                status = teld.report_status()
                if status is None or status.get('telescope_focus_state', None) is None:
                    log.error(log_name, 'Telescope is not initialized')
                    return None

                if status['telescope_focus_state'] != FocusState.Ready or 'telescope_focus_um' not in status:
                    log.error(log_name, 'Telescope focuser is not ready')
                    return None

                return status['telescope_focus_um']
        except Pyro4.errors.CommunicationError:
            log.error(log_name, 'Failed to communicate with telescope daemon')
            return None
        except Exception:
            log.error(log_name, 'Unknown error while querying telescope focus')
            traceback.print_exc(file=sys.stdout)
            return None

    # TODO: Support red focuser
    return None


def focus_set(log_name, camera_id, position, timeout=FOCUS_TIMEOUT):
    """Set the given focuser channel to the given position"""
    if camera_id == 'blue':
        try:
            with daemons.onemetre_telescope.connect(timeout=timeout) as teld:
                if teld.telescope_focus(position) != TelCommandStatus.Succeeded:
                    log.error(log_name, 'Failed to set focuser position')
                    return False
                return True
        except Pyro4.errors.CommunicationError:
            log.error(log_name, 'Failed to communicate with telescope daemon')
            return False
        except Exception:
            log.error(log_name, 'Unknown error while setting telescope focus')
            traceback.print_exc(file=sys.stdout)
            return False

    # TODO: Support red focuser
    return False
