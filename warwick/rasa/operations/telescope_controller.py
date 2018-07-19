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

"""Class managing automatic telescope control for the operations daemon"""

# pylint: disable=too-many-instance-attributes
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches

import collections
import datetime
import threading
from warwick.observatory.common import log

from .actions import Initialize, Shutdown, TelescopeActionStatus
from .constants import OperationsMode

# This should be kept in sync with the dictionary in ops
class CameraStatus:
    """Camera status, from camd"""
    Disabled, Initializing, Idle, Acquiring, Reading, Aborting = range(6)

class TelescopeController(object):
    """Class managing automatic telescope control for the operations daemon"""
    def __init__(self, loop_delay=5):
        self._wait_condition = threading.Condition()
        self._loop_delay = loop_delay

        self._action_lock = threading.Lock()
        self._action_queue = collections.deque()
        self._active_action = None
        self._initialized = False

        self._status_updated = datetime.datetime.utcnow()

        self._lock = threading.Lock()
        self._mode = OperationsMode.Manual
        self._mode_updated = datetime.datetime.utcnow()
        self._requested_mode = OperationsMode.Manual

        self._log_name = 'rasa_opsd'

        self._action_count = 0
        self._current_action_number = 0

        self._run_thread = threading.Thread(target=self.__run)
        self._run_thread.daemon = True
        self._run_thread.start()

    def __run(self):
        while True:
            with self._action_lock:
                auto_failure = self._mode == OperationsMode.Error and \
                    self._requested_mode == OperationsMode.Automatic

                if self._requested_mode != self._mode and not auto_failure:
                    print('telescope: changing mode from ' + OperationsMode.Names[self._mode] + \
                        ' to ' + OperationsMode.Names[self._requested_mode])

                    # When switching to manual mode we abort the queue
                    # but must wait for the current action to clean itself
                    # up and finish before changing _mode
                    if self._requested_mode == OperationsMode.Manual:
                        if self._action_queue:
                            print('switching to manual mode - aborting queue')
                            if self._active_action is not None:
                                self._active_action.abort()
                            log.info(self._log_name, 'Aborting action queue')
                            self._action_queue.clear()
                            self._action_count = self._current_action_number = 0
                        elif self._active_action is None:
                            print('queue aborted - switching to manual')
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
                                self._active_action = Initialize()
                            else:
                                self._active_action = self._action_queue.pop()
                                self._current_action_number += 1
                        # We have nothing left to do, so stow the telescope until next time
                        elif not self._action_queue and self._initialized and \
                                self._requested_mode != OperationsMode.Manual:
                            self._active_action = Shutdown()
                            self._action_count = self._current_action_number = 0

                        # Start the action running
                        if self._active_action is not None:
                            self._active_action.start()

                    if self._active_action is not None:
                        # Poll the current action until it completes or encounters an error
                        # Query the status into a variable here to avoid race conditions
                        status = self._active_action.status
                        if status == TelescopeActionStatus.Error:
                            print('action is error - aborting queue')
                            log.error(self._log_name, 'Action failed: ' + self._active_action.name)
                            log.info(self._log_name, 'Aborting action queue and parking telescope')
                            self._action_queue.clear()
                            self._mode = OperationsMode.Error
                            self._action_count = self._current_action_number = 0

                        if status != TelescopeActionStatus.Incomplete:
                            if isinstance(self._active_action, Initialize):
                                print('Initialization complete')
                                self._initialized = True
                            elif isinstance(self._active_action, Shutdown):
                                print('Shutdown complete')
                                self._initialized = False

                            self._active_action = None
                            continue

            # Wait for the next loop period, unless woken up early by __shortcut_loop_wait
            with self._wait_condition:
                self._wait_condition.wait(self._loop_delay)

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
        """Append TelescopeActions to the action queue
           Returns True on success or False if the
           telescope is not under automatic control.
        """
        print('queue actions')
        with self._action_lock:
            if self._mode != OperationsMode.Automatic:
                print('not automatic')
                return False

            for action in actions:
                print('queuing', action.name)
                self._action_queue.appendleft(action)
                self._action_count += 1
            self.__shortcut_loop_wait()
        return True

    def notify_processed_frame(self, headers):
        """Called by the pipeline daemon to notify that a new frame has completed processing
           headers is a dictionary holding the key-value pairs from the fits header"""
        with self._action_lock:
            if self._active_action:
                if self._active_action.status == TelescopeActionStatus.Incomplete:
                    self._active_action.received_frame(headers)

    def abort(self):
        """Placeholder logic to cancel the active telescope task"""
        with self._action_lock:
            if self._active_action:
                self._action_queue.clear()
                self._active_action.abort()
                self._action_count = self._current_action_number = 0
