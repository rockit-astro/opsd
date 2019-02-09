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

"""Base telescope action that is extended by other actions"""

# pylint: disable=too-few-public-methods
# pylint: disable=bare-except
# pylint: disable=too-many-instance-attributes

import sys
import threading
import traceback
from warwick.observatory.common import log

class TelescopeActionStatus:
    """Constants representing the status of a telescope action"""
    Incomplete, Complete, Error = range(3)

class TelescopeAction(object):
    """Base telescope action that is extended by other actions"""
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.task = None
        self.log_name = 'rasa_opsd'

        # The current status of the action, queried by the controller thread
        # This should only change to Complete or Error immediately before
        # exiting the run thread
        self.status = TelescopeActionStatus.Incomplete
        self.aborted = False

        # The object is created when the night is scheduled
        # Defer the run thread creation until the action first ticks
        self._run_thread = None

        # Set when the action is started or by notification
        self.dome_is_open = False

    @classmethod
    def validation_schema(cls):
        """Returns the schema to use for validating input configuration"""
        return None

    def set_task(self, task):
        """Updates the task shown to the user"""
        self.task = task

    def start(self, dome_is_open):
        """Spawns the run thread that runs the hardware actions"""
        # Start the run thread on the first tick
        self.dome_is_open = dome_is_open
        if self._run_thread is None:
            self._run_thread = threading.Thread(target=self.__run_thread_wrapper)
            self._run_thread.daemon = True
            self._run_thread.start()

    def __run_thread_wrapper(self):
        """Wrapper that catches exceptions thrown in run_thread implementations
           and sets the error status
        """
        try:
            self.run_thread()
        except:
            print('error: exception in action run thread:')
            traceback.print_exc(file=sys.stdout)
            log.error(self.log_name, 'Exception in action run thread')
            self.status = TelescopeActionStatus.Error

    def run_thread(self):
        """Thread that runs the hardware actions
           All actions that interact with hardware should run from here
        """
        # Dummy implementation that succeeds immediately
        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        self.aborted = True

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        self.dome_is_open = dome_is_open

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        pass
