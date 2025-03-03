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

"""Class managing automatic dome control for the operations daemon"""

# pylint: disable=too-many-branches

import threading
from astropy.time import Time
import astropy.units as u
from rockit.common import log
from .constants import DomeStatus, OperationsMode


class DomeController:
    """Class managing automatic dome control for the operations daemon"""
    def __init__(self, config):
        self._config = config
        self._lock = threading.Lock()
        self._wait_condition = threading.Condition()

        self._daemon_error = False

        self._mode = OperationsMode.Manual
        self._mode_updated = Time.now()
        self._requested_mode = OperationsMode.Manual
        self._requested_open_date = None
        self._requested_close_date = None

        self._status = DomeStatus.Closed
        self._status_updated = Time.now()

        self._environment_safe = False
        self._environment_safe_date = Time('2000-01-01T12:00:00')

        self._dome_interface = config.dome_interface_type(config.dome_json)

        loop = threading.Thread(target=self.__loop)
        loop.daemon = True
        loop.start()

    def __set_status(self, status):
        """Updates the dome status and resets the last updated time"""
        with self._lock:
            self._status = status
            self._status_updated = Time.now()

    def __set_mode(self, mode):
        """Updates the dome control mode and resets the last updated time"""
        with self._lock:
            self._mode = mode
            self._mode_updated = Time.now()

    def __loop(self):
        """Thread that controls dome opening/closing to match requested state"""
        while True:
            # Handle requests from the user to change between manual and automatic mode
            # Manual intervention is required to clear errors and return to automatic mode.
            # If an error does occur the dome heartbeat will timeout, and it will close itself.

            # Copy public facing variables to avoid race conditions
            with self._lock:
                requested_mode = self._requested_mode

                current_date = Time.now()
                requested_open = self._requested_open_date is not None and \
                                 self._requested_close_date is not None and \
                                 self._requested_open_date < current_date < self._requested_close_date and \
                                 self._environment_safe and \
                                 self._environment_safe_date > self._requested_open_date

                requested_status = DomeStatus.Open if requested_open else DomeStatus.Closed
                environment_safe_age = (current_date - self._environment_safe_date).to_value(u.s)

            auto_failure = self._mode == OperationsMode.Error and \
                requested_mode == OperationsMode.Automatic

            if requested_mode != self._mode and not auto_failure:
                print('dome: changing mode from ' + OperationsMode.label(self._mode) +
                      ' to ' + OperationsMode.label(requested_mode))

                try:
                    if requested_mode == OperationsMode.Automatic:
                        if self._dome_interface.ping_heartbeat():
                            self.__set_mode(OperationsMode.Automatic)
                            log.info(self._config.log_name, 'Dome switched to Automatic mode')
                        else:
                            self.__set_mode(OperationsMode.Error)
                            log.info(self._config.log_name, 'Failed to switch dome to Automatic mode')
                    else:
                        if self._dome_interface.disable_heartbeat():
                            self.__set_mode(OperationsMode.Manual)
                            log.info(self._config.log_name, 'Dome switched to Manual mode')
                        else:
                            self.__set_mode(OperationsMode.Error)
                            log.error(self._config.log_name, 'Failed to switch dome to Manual mode')
                    if self._daemon_error:
                        log.info(self._config.log_name, 'Restored contact with Dome daemon')
                except Exception:
                    if not self._daemon_error:
                        log.error(self._config.log_name, 'Lost contact with Dome daemon')
                        self._daemon_error = True

                    self.__set_mode(OperationsMode.Error)

            if self._mode == OperationsMode.Automatic:
                try:
                    status = self._dome_interface.query_status()
                    self.__set_status(status)

                    if status == DomeStatus.Timeout:
                        print('dome: detected heartbeat timeout!')
                        self.__set_mode(OperationsMode.Error)
                    elif requested_status == DomeStatus.Closed and status == DomeStatus.Open:
                        self.__set_status(DomeStatus.Moving)
                        if self._dome_interface.close():
                            self.__set_status(DomeStatus.Closed)
                        else:
                            self.__set_mode(OperationsMode.Error)
                    elif requested_status == DomeStatus.Open and status == DomeStatus.Closed:
                        self.__set_status(DomeStatus.Moving)
                        if self._dome_interface.open():
                            self.__set_status(DomeStatus.Open)
                        else:
                            self.__set_mode(OperationsMode.Error)
                    elif requested_status == status and environment_safe_age < 30:
                        self._dome_interface.ping_heartbeat()
                except Exception:
                    if not self._daemon_error:
                        log.error(self._config.log_name, 'Lost contact with Dome daemon')
                        self._daemon_error = True

                    self.__set_mode(OperationsMode.Error)

            # Clear the schedule if we have passed the close date
            if self._requested_close_date and current_date > self._requested_close_date:
                self.clear_open_window()

            # Wait for the next loop period, unless woken up early by __shortcut_loop_wait
            with self._wait_condition:
                self._wait_condition.wait(self._config.loop_delay)

    def __shortcut_loop_wait(self):
        """Makes the run loop continue immediately if it is currently sleeping"""
        with self._wait_condition:
            self._wait_condition.notify_all()

    def status(self):
        """Returns a dictionary with the current dome status"""
        with self._lock:
            open_str = None
            if self._requested_open_date:
                open_str = self._requested_open_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            close_str = None
            if self._requested_close_date:
                close_str = self._requested_close_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            return {
                'mode': self._mode,
                'mode_updated': self._mode_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'status': self._status,
                'status_updated': self._status_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'requested_mode': self._requested_mode,
                'requested_open_date': open_str,
                'requested_close_date': close_str,
            }

    def request_mode(self, mode):
        """Request a dome mode change (automatic/manual)"""
        with self._lock:
            self._requested_mode = mode
            self.__shortcut_loop_wait()

    def set_open_window(self, dates):
        """
        Sets the datetimes that the dome should open and close
        These dates will be cleared automatically if a weather alert triggers
        """
        if self._mode != OperationsMode.Automatic:
            return False

        if not dates or len(dates) < 2:
            return False

        if not isinstance(dates[0], Time) or not isinstance(dates[1], Time):
            return False

        with self._lock:
            self._requested_open_date = dates[0]
            self._requested_close_date = dates[1]

            open_str = dates[0].strftime('%Y-%m-%dT%H:%M:%SZ')
            close_str = dates[1].strftime('%Y-%m-%dT%H:%M:%SZ')
            log.info(self._config.log_name, 'Scheduled dome window ' + open_str + ' - ' + close_str)

            self.__shortcut_loop_wait()
            return True

    def clear_open_window(self):
        """
        Clears the times that the dome should be automatically open
        The dome will automatically close if it is currently within this window
        """
        self._requested_open_date = self._requested_close_date = None
        log.info(self._config.log_name, 'Cleared dome window')
        self.__shortcut_loop_wait()

    def notify_environment_status(self, is_safe):
        """
        Called by the environment monitor to notify the current environment status
        The dome will only open once a safe ping is received inside the open window
        The heartbeat ping will only be sent if the environment was pinged within
        the last 30 seconds
        """
        now = Time.now()
        self._environment_safe = is_safe
        self._environment_safe_date = now

        # Clear the dome schedule (forcing it to close) if the night has started
        if not is_safe and self._requested_open_date and now > self._requested_open_date:
            self.clear_open_window()
