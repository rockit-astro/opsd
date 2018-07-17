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

"""Telescope action to slew the telescope to a given ra, dec"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import math
import sys
import traceback
import Pyro4
from warwick.observatory.common import (
    daemons,
    log)
from warwick.rasa.telescope import CommandStatus as TelCommandStatus

from . import TelescopeAction, TelescopeActionStatus

SLEW_TIMEOUT = 120

def tel_slew_radec(log_name, ra, dec, tracking, slew_timeout):
    """Slew the telescope to a given RA, Dec"""
    try:
        with daemons.rasa_telescope.connect(timeout=slew_timeout) as teld:
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

class SlewTelescope(TelescopeAction):
    """Telescope action to slew the telescope to a given ra, dec"""
    def __init__(self, config):
        super().__init__('Slew Telescope', config)

    @classmethod
    def validation_schema(cls):
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['ra', 'dec', 'tracking'],
            'properties': {
                'type': {'type': 'string'},
                'ra': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 2 * math.pi
                },
                'dec': {
                    'type': 'number',
                    'minimum': -math.pi / 2,
                    'maximum': math.pi / 2
                },
                'tracking': {
                    'type': 'boolean'
                }
            }
        }

    def run_thread(self):
        """Thread that runs the hardware actions"""
        self.set_task('Slewing')
        if not tel_slew_radec(self.log_name, self.config['ra'], self.config['dec'],
                              self.config['tracking'], SLEW_TIMEOUT):
            self.status = TelescopeActionStatus.Error
            return

        if not self.aborted:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def abort(self):
        """Aborted by a weather alert or user action"""
        super().abort()
        tel_stop(self.log_name)
