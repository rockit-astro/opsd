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

"""Class managing automatic dome control for the operations daemon"""

# pylint: disable=too-few-public-methods
# pylint: disable=too-many-statements
# pylint: disable=too-many-branches
# pylint: disable=too-many-instance-attributes
# pylint: disable=invalid-name
# pylint: disable=broad-except

import datetime
import threading
import time

from warwick.observatory.common import log
from warwick.w1m.dome import (
    CommandStatus as DomeCommandStatus,
    DomeShutterStatus,
    DomeHeartbeatStatus)

from .constants import OperationsMode

class DomeStatus:
    """Aggregated dome status"""
    Closed, Open, Moving, Timeout = range(4)
    Names = ['Closed', 'Open', 'Moving', 'Timeout']

    @classmethod
    def parse(cls, status):
        """Parses the return value from dome.status() into a DomeStatus"""
        if status['heartbeat_status'] in [DomeHeartbeatStatus.TrippedClosing,
                                          DomeHeartbeatStatus.TrippedIdle]:
            return DomeStatus.Timeout

        if status['east_shutter'] == DomeShutterStatus.Closed and \
                status['west_shutter'] == DomeShutterStatus.Closed:
            return DomeStatus.Closed

        if status['east_shutter'] == DomeShutterStatus.Open and \
                status['west_shutter'] == DomeShutterStatus.Open:
            return DomeStatus.Open

        return DomeStatus.Moving

class DomeController(object):
    """Class managing automatic dome control for the operations daemon"""
    def __init__(self, daemon, open_close_timeout, heartbeat_timeout, loop_delay=10):
        self._lock = threading.Lock()
        self._daemon = daemon
        self._open_close_timeout = open_close_timeout
        self._heartbeat_timeout = heartbeat_timeout
        self._loop_delay = loop_delay
        self._daemon_error = False

        self._mode = OperationsMode.Manual
        self._mode_updated = datetime.datetime.utcnow()
        self._requested_mode = OperationsMode.Manual

        self._status = DomeStatus.Closed
        self._status_updated = datetime.datetime.utcnow()
        self._requested_status = DomeStatus.Closed

        loop = threading.Thread(target=self.__loop)
        loop.daemon = True
        loop.start()

    def __set_status(self, status):
        """Updates the dome status and resets the last updated time"""
        with self._lock:
            self._status = status
            self._status_updated = datetime.datetime.utcnow()

    def __set_mode(self, mode):
        """Updates the dome control mode and resets the last updated time"""
        with self._lock:
            self._mode = mode
            self._mode_updated = datetime.datetime.utcnow()

    def __loop(self):
        """Thread that controls dome opening/closing to match requested state"""
        while True:
            # Handle requests from the user to change between manual and automatic mode
            # Manual intervention is required to clear errors and return to automatic mode.
            # If an error does occur the dome heartbeat will timeout, and it will close itself.

            # Copy public facing variables to avoid race conditions
            with self._lock:
                requested_mode = self._requested_mode
                requested_status = self._requested_status

            auto_failure = self._mode == OperationsMode.Error and \
                requested_mode == OperationsMode.Automatic

            if requested_mode != self._mode and not auto_failure:
                print('dome: changing mode from ' + OperationsMode.Names[self._mode] + \
                    ' to ' + OperationsMode.Names[requested_mode])

                try:
                    with self._daemon.connect() as dome:
                        if requested_mode == OperationsMode.Automatic:
                            # TODO: Change this to lock to ops mode
                            ret = dome.set_heartbeat_timer(self._heartbeat_timeout)
                            if ret == DomeCommandStatus.Succeeded:
                                self.__set_mode(OperationsMode.Automatic)
                                log.info('opsd', 'Dome switched to Automatic mode')
                            else:
                                print('error: failed to switch dome to auto with ' \
                                    + DomeCommandStatus.message(ret))
                                self.__set_mode(OperationsMode.Error)
                                log.info('opsd', 'Failed to switch dome to Automatic mode')
                        else:
                            # Switch from auto or error state back to manual
                            # TODO: Change this to unlock from ops mode
                            # TODO: the dome daemon should disable the ops lock if it times out
                            ret = dome.set_heartbeat_timer(0)

                            if ret == DomeCommandStatus.Succeeded:
                                self.__set_mode(OperationsMode.Manual)
                                log.error('opsd', 'Dome switched to Manual mode')
                            else:
                                print('error: failed to switch dome to manual with ' \
                                    + DomeCommandStatus.message(ret))
                                self.__set_mode(OperationsMode.Error)
                                log.error('opsd', 'Failed to switch dome to Manual mode')
                    if self._daemon_error:
                        log.info('opsd', 'Restored contact with Dome daemon')
                except Exception as e:
                    if not self._daemon_error:
                        log.error('opsd', 'Lost contact with Dome daemon')
                        self._daemon_error = True

                    print('error: failed to communicate with the dome daemon: ', e)
                    self.__set_mode(OperationsMode.Error)

            if self._mode == OperationsMode.Automatic:
                try:
                    with self._daemon.connect() as dome:
                        status = DomeStatus.parse(dome.status())
                        self.__set_status(status)

                    print('dome: is ' +  DomeStatus.Names[status] + ' and wants to be ' + \
                        DomeStatus.Names[requested_status])

                    if status == DomeStatus.Timeout:
                        print('dome: detected heartbeat timeout!')
                        self.__set_mode(OperationsMode.Error)
                    elif requested_status == DomeStatus.Closed and status == DomeStatus.Open:
                        print('dome: sending heartbeat ping before closing')
                        with self._daemon.connect() as dome:
                            dome.set_heartbeat_timer(self._heartbeat_timeout)
                        print('dome: closing')
                        self.__set_status(DomeStatus.Moving)
                        with self._daemon.connect(timeout=self._open_close_timeout) as dome:
                            ret = dome.close_shutters(east=True, west=True)
                        if ret == DomeCommandStatus.Succeeded:
                            self.__set_status(DomeStatus.Closed)
                        else:
                            self.__set_mode(OperationsMode.Error)
                    elif requested_status == DomeStatus.Open and status == DomeStatus.Closed:
                        print('dome: sending heartbeat ping before opening')
                        with self._daemon.connect() as dome:
                            dome.set_heartbeat_timer(self._heartbeat_timeout)
                        print('dome: opening')
                        self.__set_status(DomeStatus.Moving)
                        with self._daemon.connect(timeout=self._open_close_timeout) as dome:
                            ret = dome.open_shutters(east=True, west=True)
                        if ret == DomeCommandStatus.Succeeded:
                            self.__set_status(DomeStatus.Open)
                        else:
                            self.__set_mode(OperationsMode.Error)

                    elif requested_status == status:
                        print('dome: sending heartbeat ping')
                        with self._daemon.connect() as dome:
                            dome.set_heartbeat_timer(self._heartbeat_timeout)
                except Exception as e:
                    if not self._daemon_error:
                        log.error('opsd', 'Lost contact with Dome daemon')
                        self._daemon_error = True

                    print('error: failed to communicate with the dome daemon: ', e)
                    self.__set_mode(OperationsMode.Error)

            time.sleep(self._loop_delay)


    def status(self):
        """Returns a tuple of the current dome status (Open/Closed/Moving/etc) and update time"""
        with self._lock:
            return {
                'mode': self._mode,
                'mode_updated': self._mode_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'status': self._status,
                'status_updated': self._status_updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'requested_mode': self._requested_mode,
                'requested_status': self._requested_status,
            }

    def request_status(self, status):
        """Request a dome status change (open/closed)"""
        with self._lock:
            self._requested_status = status

    def request_mode(self, mode):
        """Request a dome mode change (automatic/manual)"""
        with self._lock:
            self._requested_mode = mode
