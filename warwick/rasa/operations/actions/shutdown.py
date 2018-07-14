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

"""Telescope action to park the telescope and switch off the drive power"""

# pylint: disable=broad-except
# pylint: disable=invalid-name

import Pyro4
from warwick.observatory.common import (
    daemons,
    log)

from . import TelescopeAction, TelescopeActionStatus
from ..constants import CommandStatus

# Position to park the telescope after homing
STOW_ALTAZ = (0.616, 0.405)
STOW_TIMEOUT = 60

class Shutdown(TelescopeAction):
    """Telescope action to park the telescope and switch off the drive power"""
    def __init__(self):
        super().__init__('Shutdown', {})

    def run_thread(self):
        """Thread that runs the hardware actions"""
        try:
            self.set_task('Parking Telescope')

            with daemons.rasa_telescope.connect(timeout=STOW_TIMEOUT) as teld:
                status = teld.slew_altaz(STOW_ALTAZ[0], STOW_ALTAZ[1])
                if status != CommandStatus.Succeeded:
                    print('Failed to park telescope')
                    log.error(self.log_name, 'Failed to park telescope')
                    self.status = TelescopeActionStatus.Error
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with telescope daemon')
            log.error(self.log_name, 'Failed to communicate with telescope daemon')
            self.status = TelescopeActionStatus.Error
        except Exception as e:
            print('Unknown error while parking telescope')
            print(e)
            log.error(self.log_name, 'Unknown error while parking telescope')
            self.status = TelescopeActionStatus.Error

        self.status = TelescopeActionStatus.Complete
