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

import queue
import threading
from astropy.time import Time
import astropy.units as u
from rockit.common import log
from .constants import DomeStatus, OperationsMode, CommandStatus


class DomeController:
    """Class managing automatic dome control for the operations daemon"""
    def __init__(self, config):
        self._config = config
        self._lock = threading.Lock()
        self._wait_condition = threading.Condition()

        self.comm_lock = threading.Lock()
        self.command_queue = queue.Queue()
        self.result_queue = queue.Queue()

        self._mode = OperationsMode.Manual
        self._mode_updated = Time.now()
        self._open_date = None
        self._close_date = None

        self._status = DomeStatus.Closed
        self._status_updated = Time.now()

        self._environment_safe = False
        self._environment_safe_date = Time('2000-01-01T12:00:00')

        self._dome_interface = config.dome_interface_type(config.dome_json)

        loop = threading.Thread(target=self.__loop)
        loop.daemon = True
        loop.start()

    def __set_error(self):
        with self._lock:
            self._mode = OperationsMode.Error
            self._open_date = self._close_date = None
            self._mode_updated = Time.now()

    def __loop(self):
        """Thread that controls dome opening/closing to match requested state"""
        while True:
            # Handle requests from the user to change between manual and automatic mode
            # Manual intervention is required to clear errors and return to automatic mode.
            # If an error does occur the dome heartbeat will timeout, and it will close itself.
            try:
                request, data = self.command_queue.get(timeout=self._config.loop_delay)
            except queue.Empty:
                request, data = None, None

            if request == 'mode':
                if data == self._mode:
                    self.result_queue.put(CommandStatus.Succeeded)
                else:
                    print('dome: changing mode from ' + OperationsMode.label(self._mode) +
                          ' to ' + OperationsMode.label(data))
                    if data == OperationsMode.Automatic:
                        ret = self._dome_interface.set_automatic()
                        if ret == CommandStatus.Succeeded:
                            with self._lock:
                                self._mode = OperationsMode.Automatic
                                self._mode_updated = Time.now()
                            log.info(self._config.log_name, 'Dome switched to Automatic mode')
                        else:
                            self.__set_error()
                            log.info(self._config.log_name, 'Failed to switch dome to Automatic mode')
                    else:
                        ret = self._dome_interface.set_manual()
                        if ret == CommandStatus.Succeeded:
                            with self._lock:
                                self._mode = OperationsMode.Manual
                                self._open_date = self._close_date = None
                                self._mode_updated = Time.now()
                            log.info(self._config.log_name, 'Dome switched to Manual mode')
                        else:
                            self.__set_error()
                            log.info(self._config.log_name, 'Failed to switch dome to Manual mode')
                    self.result_queue.put(ret)
            elif request == 'schedule':
                if self._mode != OperationsMode.Automatic:
                    self.result_queue.put(CommandStatus.DomeNotAutomatic)
                else:
                    if isinstance(data[0], Time) and isinstance(data[1], Time):
                        self._open_date, self._close_date = data
                        open_str = data[0].strftime('%Y-%m-%dT%H:%M:%SZ')
                        close_str = data[1].strftime('%Y-%m-%dT%H:%M:%SZ')
                        log.info(self._config.log_name, f'Scheduled dome window {open_str} - {close_str}')
                        self.result_queue.put(CommandStatus.Succeeded)
                    else:
                        self.result_queue.put(CommandStatus.Failed)
            elif request == 'clear':
                if self._mode != OperationsMode.Automatic:
                    self.result_queue.put(CommandStatus.DomeNotAutomatic)
                else:
                    self._open_date = self._close_date = None
                    log.info(self._config.log_name, 'Cleared dome window')
                    self.result_queue.put(CommandStatus.Succeeded)
            elif request == 'ping':
                self.result_queue.put(CommandStatus.Succeeded)
            elif request is not None:
                self.result_queue.put(CommandStatus.Failed)

            if self._mode != OperationsMode.Automatic:
                continue

            # Clear the schedule if we have passed the close date
            current_date = Time.now()
            if self._close_date and current_date > self._close_date:
                with self._lock:
                    self._open_date = self._close_date = None

            # Clear the schedule if the weather is bad
            if not self._environment_safe and not self._dome_interface.reopen_after_weather_alert and \
                    self._open_date is not None and self._environment_safe_date > self._open_date:
                with self._lock:
                    self._open_date = self._close_date = None

            should_be_open = self._open_date is not None and self._close_date is not None and \
                             self._open_date < current_date < self._close_date and \
                             self._environment_safe and self._environment_safe_date > self._open_date

            status = self._dome_interface.query_status()
            with self._lock:
                self._status = status
                self._status_updated = Time.now()

            print('dome: is ' + DomeStatus.label(status) + ' and wants to be ' +
                  DomeStatus.label(DomeStatus.Open if should_be_open else DomeStatus.Closed))

            refresh_status = True
            if status == DomeStatus.Offline:
                print('dome: dome is offline')
                self.__set_error()
            elif status == DomeStatus.Timeout:
                print('dome: detected heartbeat timeout!')
                self.__set_error()
            elif status in [DomeStatus.Open, DomeStatus.Opening] and not should_be_open:
                if not self._dome_interface.close():
                    self.__set_error()
            elif status in [DomeStatus.Closed, DomeStatus.Closing] and should_be_open:
                if not self._dome_interface.open():
                    self.__set_error()
            else:
                refresh_status = False

            if refresh_status:
                status = self._dome_interface.query_status()
                with self._lock:
                    self._status = status
                    self._status_updated = Time.now()

            environment_safe_age = (current_date - self._environment_safe_date).to_value(u.s)
            if environment_safe_age < self._dome_interface.environment_stale_limit:
                self._dome_interface.ping_heartbeat()

    def status(self):
        """Returns a dictionary with the current dome status"""
        with self._lock:
            open_str = None
            if self._open_date:
                open_str = self._open_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            close_str = None
            if self._close_date:
                close_str = self._close_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            return {
                'mode': self._mode,
                'mode_updated': self._mode_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'status': self._status,
                'status_updated': self._status_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'open_date': open_str,
                'close_date': close_str,
            }

    def set_mode(self, mode):
        """Request a dome mode change (automatic/manual)"""
        with self.comm_lock:
            self.command_queue.put(('mode', mode))
            return self.result_queue.get()

    def set_schedule(self, open, close):
        """
        Sets the datetimes that the dome should open and close
        These dates will be cleared automatically if a weather alert triggers
        """
        with self.comm_lock:
            self.command_queue.put(('schedule', (open, close)))
            return self.result_queue.get()

    def clear_schedule(self):
        """
        Clears the times that the dome should be automatically open
        """
        with self.comm_lock:
            self.command_queue.put(('clear', None))
            return self.result_queue.get()

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

        # Wake up the run loop so it can respond immediately
        with self.comm_lock:
            self.command_queue.put(('ping', None))
            self.result_queue.get()
