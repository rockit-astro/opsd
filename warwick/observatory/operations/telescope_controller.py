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

"""Class managing automatic telescope control for the operations daemon"""

# pylint: disable=too-many-branches

import collections
import datetime
import threading
from warwick.observatory.common import log

from .telescope_action import TelescopeActionStatus
from .dome_controller import DomeStatus
from .constants import OperationsMode


class TelescopeController:
    """Class managing automatic telescope control for the operations daemon"""
    def __init__(self, config, dome_controller):
        self._config = config
        self._wait_condition = threading.Condition()

        self._initialize_action = config.actions['Initialize']
        self._park_action = config.actions['ParkTelescope']

        self._action_lock = threading.Lock()
        self._action_queue = collections.deque()
        self._active_action = None
        self._initialized = False

        self._status_updated = datetime.datetime.utcnow()

        self._lock = threading.Lock()
        self._mode = OperationsMode.Manual
        self._mode_updated = datetime.datetime.utcnow()
        self._requested_mode = OperationsMode.Manual

        self._action_count = 0
        self._current_action_number = 0

        self._dome_controller = dome_controller
        self._dome_was_open = False

        self._run_thread = threading.Thread(target=self.__run)
        self._run_thread.daemon = True
        self._run_thread.start()

    def __run(self):
        while True:
            # Assume the dome is correctly set if it is in manual mode
            dome_status = self._dome_controller.status()
            dome_is_open = dome_status['status'] == DomeStatus.Open or dome_status['mode'] == OperationsMode.Manual

            with self._action_lock:
                auto_failure = self._mode == OperationsMode.Error and \
                    self._requested_mode == OperationsMode.Automatic

                if self._requested_mode != self._mode and not auto_failure:
                    print('telescope: changing mode from ' + OperationsMode.label(self._mode) +
                          ' to ' + OperationsMode.label(self._requested_mode))

                    # When switching to manual mode we abort the queue
                    # but must wait for the current action to clean itself
                    # up and finish before changing _mode
                    if self._requested_mode == OperationsMode.Manual:
                        if self._action_queue:
                            if self._active_action is not None:
                                self._active_action.abort()

                            log.info(self._config.log_name, 'Aborting action queue')
                            self._action_queue.clear()
                            self._action_count = self._current_action_number = 0
                        elif self._active_action is None:
                            self._mode = OperationsMode.Manual

                    # When switching to automatic mode we must reinitialize
                    # the telescope to make sure it is in a consistent state
                    elif self._requested_mode == OperationsMode.Automatic:
                        self._initialized = False
                        self._mode = OperationsMode.Automatic

                self._status_updated = datetime.datetime.utcnow()

                if self._mode != OperationsMode.Manual:
                    # If the active action is None then we have either just finished
                    # the last action (and should queue the next one), have just run
                    # out of actions (and should shutdown the telescope), or are idling
                    # waiting for new actions to appear (and should do nothing)
                    if self._active_action is None:
                        # We have something to do, but may need to initialize the telescope
                        if self._action_queue:
                            if not self._initialized:
                                self._active_action = self._initialize_action(self._config.log_name)
                            else:
                                self._active_action = self._action_queue.pop()
                                self._current_action_number += 1
                        # We have nothing left to do, so stow the telescope until next time
                        elif not self._action_queue and self._initialized and \
                                self._requested_mode != OperationsMode.Manual:
                            self._active_action = self._park_action(self._config.log_name)
                            self._action_count = self._current_action_number = 0

                        # Start the action running
                        if self._active_action is not None:
                            self._active_action.start(dome_is_open)

                    if self._active_action is not None:
                        # Poll the current action until it completes or encounters an error
                        # Query the status into a variable here to avoid race conditions
                        status = self._active_action.status
                        if status == TelescopeActionStatus.Error:
                            log.error(self._config.log_name, 'Action failed: ' + self._active_action.name)
                            log.info(self._config.log_name, 'Aborting action queue and parking telescope')
                            self._action_queue.clear()
                            self._mode = OperationsMode.Error
                            self._action_count = self._current_action_number = 0

                        if status == TelescopeActionStatus.Incomplete:
                            if dome_is_open != self._dome_was_open:
                                self._active_action.dome_status_changed(dome_is_open)
                        else:
                            if isinstance(self._active_action, self._initialize_action):
                                self._initialized = True
                            elif isinstance(self._active_action, self._park_action):
                                self._initialized = False

                            self._active_action = None
                            continue

            self._dome_was_open = dome_is_open

            # Wait for the next loop period, unless woken up early by __shortcut_loop_wait
            with self._wait_condition:
                self._wait_condition.wait(self._config.loop_delay)

    def __shortcut_loop_wait(self):
        """Makes the run loop continue immediately if it is currently sleeping"""
        with self._wait_condition:
            self._wait_condition.notify_all()

    def status(self):
        """Returns a dictionary with the current telescope status"""
        with self._action_lock:
            ret = {
                'mode': self._mode,
                'mode_updated': self._mode_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'requested_mode': self._requested_mode,
                'status_updated': self._status_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'action_number': self._current_action_number,
                'action_count': self._action_count
            }

            if self._active_action is not None:
                ret.update({
                    'action_name': self._active_action.name,
                    'action_task': self._active_action.task,
                    'action_status': self._active_action.status
                })

            return ret

    def request_mode(self, mode):
        """Request a telescope mode change (automatic/manual)"""
        with self._action_lock:
            self._requested_mode = mode
            self.__shortcut_loop_wait()

    def queue_actions(self, actions):
        """
        Append TelescopeActions to the action queue
        Returns True on success or False if the
        telescope is not under automatic control.
        """
        with self._action_lock:
            if self._mode != OperationsMode.Automatic:
                return False

            for action in actions:
                self._action_queue.appendleft(action)
                self._action_count += 1
            self.__shortcut_loop_wait()
        return True

    def notify_processed_frame(self, headers):
        """
        Called by the pipeline daemon to notify that a new frame has completed processing
        headers is a dictionary holding the key-value pairs from the fits header
        """
        with self._action_lock:
            if self._active_action:
                if self._active_action.status == TelescopeActionStatus.Incomplete:
                    self._active_action.received_frame(headers)

    def notify_guide_profile(self, headers, profile_x, profile_y):
        """
        Called by the pipeline daemon to notify that a new guide profile has been calculated
        headers is a dictionary holding the key-value pairs from the fits header
        profile_x and profile_y are numpy arrays holding the collapsed profiles
        """
        with self._action_lock:
            if self._active_action:
                if self._active_action.status == TelescopeActionStatus.Incomplete:
                    self._active_action.received_guide_profile(headers, profile_x, profile_y)

    def abort(self):
        """Placeholder logic to cancel the active telescope task"""
        with self._action_lock:
            if self._active_action:
                self._action_queue.clear()
                self._active_action.abort()
                self._action_count = self._current_action_number = 0
