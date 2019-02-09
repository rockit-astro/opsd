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

"""Class managing automatic dehumidifier control for the operations daemon"""

# pylint: disable=too-few-public-methods
# pylint: disable=too-many-statements
# pylint: disable=too-many-branches
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-arguments
# pylint: disable=invalid-name
# pylint: disable=broad-except

import datetime
import sys
import threading
import traceback

from warwick.observatory.common import log
from warwick.observatory.power import SwitchStatus
from .constants import OperationsMode

# Humidity limits for automatic dehumidifier control
DEHUMIDIFIER_CONFIG = {
    'enable_above': 70,
    'disable_below': 65
}

class DehumidifierController(object):
    """Class managing automatic dome control for the operations daemon"""
    def __init__(self, power_daemon, log_name):
        self._power_daemon = power_daemon
        self._log_name = log_name

        self._lock = threading.Lock()
        self._humidity_error = False
        self._power_error = False
        self._active = False
        self._active_updated = datetime.datetime.utcnow()

        self._mode = OperationsMode.Automatic
        self._mode_updated = datetime.datetime.utcnow()
        self._requested_mode = OperationsMode.Automatic

    def status(self):
        """Returns a dictionary with the current dome status"""
        with self._lock:
            return {
                'mode': self._mode,
                'mode_updated': self._mode_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'active': self._active,
                'active_updated': self._active_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'requested_mode': self._requested_mode,
            }

    def request_mode(self, mode):
        """Request a dome mode change (automatic/manual)"""
        with self._lock:
            self._requested_mode = mode

    def notify_environment_status(self, internal_humidity, external_humidity, dome_open):
        """Called by the enviroment monitor to notify the current environment status
        """
        with self._lock:
            if self._mode != self._requested_mode:
                self._mode = self._requested_mode
                self._mode_updated = datetime.datetime.utcnow()

        if self._mode != OperationsMode.Automatic:
            return

        # Use external sensors if internal humidity isn't available
        humidity = internal_humidity or external_humidity

        if humidity is None:
            if not self._humidity_error:
                log.warning(self._log_name, 'Lost contact with all humidity sensors')
                self._humidity_error = True

            # Can't do anything more without valid humidity data
            return

        if self._humidity_error:
            log.info(self._log_name, 'Restored contact with humidity sensor')
            self._humidity_error = False

        # Implement hysteresis to avoid flickering on/off when humidity is on the limit
        # Also force-disable when the observing condition is set
        limit = 'disable_below' if self._active else 'enable_above'
        active = not dome_open and humidity > DEHUMIDIFIER_CONFIG[limit]

        if active != self._active:
            if active:
                log.warning(self._log_name, 'Dehumidifier enabled (humidity: {}% > {}%)'.format(
                    humidity, DEHUMIDIFIER_CONFIG[limit]))
            else:
                log.info(self._log_name, 'Dehumidifier disabled (humidity: {}% < {}%)'.format(
                    humidity, DEHUMIDIFIER_CONFIG[limit]))

        try:
            with self._power_daemon.connect() as power:
                dehumidifier_power = power.value('dehumidifier')
                if dehumidifier_power == SwitchStatus.Unknown:
                    raise Exception("Dehumidifier status unknown")

                if (dehumidifier_power == SwitchStatus.On) != self._active:
                    success = power.switch('dehumidifier', self._active)
                    if not success:
                        raise Exception('Failed to switch dehumidifier')

            if self._power_error:
                log.info(self._log_name, 'Restored contact with dehumidifier')
                self._power_error = False
        except Exception:
            print('error: failed to update dehumidifier:')
            traceback.print_exc(file=sys.stdout)
            if not self._power_error:
                log.error(self._log_name, 'Lost contact with dehumidifier')
            self._power_error = True

        with self._lock:
            self._active = active
            self._active_updated = datetime.datetime.utcnow()
