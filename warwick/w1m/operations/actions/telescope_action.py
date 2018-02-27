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

"""Base telescope action that is extended by other actions"""

# pylint: disable=too-few-public-methods

import threading

class TelescopeActionStatus:
    """Constants representing the status of a telescope action"""
    Incomplete, Complete, Error = range(3)

class TelescopeAction(object):
    """Base telescope action that is extended by other actions"""
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.task = None

        # The current status of the action, queried by the controller thread
        # This should only change to Complete or Error immediately before
        # exiting the run thread
        self.status = TelescopeActionStatus.Incomplete
        self.aborted = False

        # The object is created when the night is scheduled
        # Defer the run thread creation until the action first ticks
        self._run_thread = None

    @classmethod
    def validation_schema(cls):
        """Returns the schema to use for validating input configuration"""
        return None

    def set_task(self, task):
        """Updates the task shown to the user"""
        self.task = task

    def start(self):
        """Spawns the run thread that runs the hardware actions"""
        # Start the run thread on the first tick
        if self._run_thread is None:
            self._run_thread = threading.Thread(target=self.run_thread)
            self._run_thread.daemon = True
            self._run_thread.start()

    def run_thread(self):
        """Thread that runs the hardware actions
           All actions that interact with hardware should run from here
        """
        # Dummy implementation that succeeds immediately
        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Aborted by a weather alert or user action"""
        self.aborted = True

    def received_frame(self, headers):
        """Received a frame from the pipeline"""
        pass
