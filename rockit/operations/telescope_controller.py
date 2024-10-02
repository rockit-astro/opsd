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

"""Class managing automatic telescope control for the operations daemon"""

# pylint: disable=too-many-branches

import collections
import threading
from astropy.time import Time
from rockit.common import log
from .telescope_action import TelescopeActionStatus
from .dome_controller import DomeStatus
from .constants import OperationsMode


class TelescopeController:
    """Class managing automatic telescope control for the operations daemon"""
    def __init__(self, config, dome_controller, environment):
        self._config = config
        self._wait_condition = threading.Condition()
        self._park_action = config.actions['ParkTelescope']

        self._action_lock = threading.Lock()
        self._action_queue = collections.deque()
        self._active_action = None
        self._idle = True

        self._status_updated = Time.now()

        self._lock = threading.Lock()
        self._mode = OperationsMode.Manual
        self._requested_mode = OperationsMode.Manual

        self._dome_controller = dome_controller
        self._environment = environment
        self._dome_was_open = False

        self._run_thread = threading.Thread(target=self.__run)
        self._run_thread.daemon = True
        self._run_thread.start()

    def __run(self):
        while True:
            if self._dome_controller is not None:
                dome_status = self._dome_controller.status()
                # Assume the dome is correctly set if it is in manual mode
                dome_is_open = dome_status['status'] == DomeStatus.Open or dome_status['mode'] == OperationsMode.Manual
            else:
                dome_is_open = self._environment.safe

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
                        elif self._active_action is None:
                            self._mode = OperationsMode.Manual

                    elif self._requested_mode == OperationsMode.Automatic:
                        self._mode = OperationsMode.Automatic

                self._status_updated = Time.now()

                if self._mode != OperationsMode.Manual:
                    # If the active action is None then we have either just finished
                    # the last action (and should queue the next one), have just run
                    # out of actions (and should shutdown the telescope), or are idling
                    # waiting for new actions to appear (and should do nothing)
                    if self._active_action is None:
                        if self._action_queue:
                            self._idle = False
                            self._active_action = self._action_queue.pop()
                        # We have nothing left to do, so stow the telescope until next time
                        elif not self._action_queue and not self._idle and \
                                self._requested_mode != OperationsMode.Manual:
                            self._active_action = self._park_action(
                                log_name=self._config.log_name,
                                site_location=self._config.site_location
                            )

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

                        if status == TelescopeActionStatus.Incomplete:
                            if dome_is_open != self._dome_was_open:
                                self._active_action.dome_status_changed(dome_is_open)
                        else:
                            if isinstance(self._active_action, self._park_action):
                                self._idle = True

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
            schedule = []
            if self._active_action and self._active_action.status == TelescopeActionStatus.Incomplete:
                schedule.append({
                    'name': self._active_action.name,
                    'tasks': self._active_action.task_labels()
                })

            action_count = len(self._action_queue)
            for i in range(action_count):
                action = self._action_queue[action_count - i - 1]
                schedule.append({
                    'name': action.name,
                    'tasks': action.task_labels()
                })

            return {
                'mode': self._mode,
                'requested_mode': self._requested_mode,
                'status_updated': self._status_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'schedule': schedule
            }

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

            self.__shortcut_loop_wait()
        return True

    def notify_processed_frame(self, headers):
        """
        Called by the pipeline daemon to notify that a new frame has completed processing.
        headers is a dictionary holding the key-value pairs from the fits header.

        Returns a list of additional header card definitions that are included in saved images.
        """
        with self._action_lock:
            if self._active_action:
                if self._active_action.status == TelescopeActionStatus.Incomplete:
                    return self._active_action.received_frame(headers)
            return None

    def notify_guide_profile(self, headers, profile_x, profile_y):
        """
        Called by the pipeline daemon to notify that a new guide profile has been calculated
        headers is a dictionary holding the key-value pairs from the fits header.

        profile_x and profile_y are numpy arrays holding the collapsed profiles.

        Returns a list of additional header card definitions that are included in saved images.
        """
        with self._action_lock:
            if self._active_action:
                if self._active_action.status == TelescopeActionStatus.Incomplete:
                    return self._active_action.received_guide_profile(headers, profile_x, profile_y)
            return None

    def abort(self):
        """Placeholder logic to cancel the active telescope task"""
        with self._action_lock:
            if self._active_action:
                self._action_queue.clear()
                self._active_action.abort()
