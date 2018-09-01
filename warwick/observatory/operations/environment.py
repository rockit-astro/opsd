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

"""Class managing the environment status checks"""

# pylint: disable=too-few-public-methods
# pylint: disable=too-many-statements
# pylint: disable=too-many-branches
# pylint: disable=too-many-instance-attributes
# pylint: disable=invalid-name
# pylint: disable=broad-except

import datetime
import sys
import threading
import traceback

from warwick.observatory.common import log

class ConditionStatus:
    """Represents the status of a condition type"""
    Unknown, Safe, Warning, Unsafe = range(4)
    Names = ['Unknown', 'Safe', 'Warning', 'Unsafe']

class ConditionWatcher:
    """Represents a parameter source for a condition flag"""
    def __init__(self, condition, device, parameter, name):
        self.name = name
        self.condition = condition
        self.status = ConditionStatus.Unknown
        self._device = device
        self._parameter = parameter

    def update(self, data):
        """Updates the condition status based on the given environment data"""
        self.status = ConditionStatus.Unknown
        if self._device in data and self._parameter in data[self._device]:
            param = data[self._device][self._parameter]
            if param['unsafe']:
                self.status = ConditionStatus.Unsafe
            elif param['warning']:
                self.status = ConditionStatus.Warning
            elif param['current']:
                self.status = ConditionStatus.Safe

    def latest(self, data):
        """Returns the latest value of the parameter, or None if it is not current"""
        if self._device in data and self._parameter in data[self._device]:
            param = data[self._device][self._parameter]
            if param['current']:
                return param['latest']
        return None

class EnvironmentWatcher(object):
    '''Class that handles parsing and exposing the data from environmentd'''
    def __init__(self, daemon, log_name, conditions):
        self._daemon = daemon
        self.safe = False
        self.wants_dehumidifier = False
        self.updated = datetime.datetime.utcnow()
        self._lock = threading.Lock()
        self._log_name = log_name

        self._conditions = {}
        for c in conditions:
            if c.condition not in self._conditions:
                self._conditions[c.condition] = []

            self._conditions[c.condition].append(c)

        self.unsafe_conditions = list(self._conditions.keys())

        # Used by dehumidifier controller
        self.internal_humidity = None
        self.external_humidity = None

    def update(self):
        '''Queries environmentd for new data and updates flags'''
        was_safe = self.safe
        try:
            with self._daemon.connect() as environment:
                data = environment.status()

            safe = True
            unsafe_conditions = []
            for condition, watchers in self._conditions.items():
                for w in watchers:
                    w.update(data)

                # Condition is considered unsafe if all parameters are unknown
                # or if any condition is unsafe
                if all([w.status == ConditionStatus.Unknown for w in watchers]) or \
                       any([w.status == ConditionStatus.Unsafe for w in watchers]):
                    safe = False
                    unsafe_conditions.append(condition)

            internal_humidity = None
            for watcher in self._conditions['internal_humidity']:
                internal_humidity = watcher.latest(data)
                if internal_humidity is not None:
                    break

            external_humidity = None
            for watcher in self._conditions['humidity']:
                external_humidity = watcher.latest(data)
                if external_humidity is not None:
                    break

            with self._lock:
                self.safe = safe
                self.unsafe_conditions = unsafe_conditions
                self.internal_humidity = internal_humidity
                self.external_humidity = external_humidity
                self.updated = datetime.datetime.utcnow()

            if was_safe and not safe:
                message = 'Environment has become unsafe (' + ', '.join(unsafe_conditions) + ')'
                print(message)
                log.warning(self._log_name, message)
            elif not was_safe and safe:
                print('Environment trigger timed out')
                log.info(self._log_name, 'Environment trigger timed out')
        except Exception as e:
            with self._lock:
                self.safe = False
                for condition, watchers in self._conditions.items():
                    for w in watchers:
                        w.update({})
                self.unsafe_conditions = list(self._conditions.keys())
                self.internal_humidity = None
                self.external_humidity = None
                self.updated = datetime.datetime.utcnow()

            print('error: failed to query environment:')
            traceback.print_exc(file=sys.stdout)
            log.info(self._log_name, 'Failed to query environment (' + str(e) + ')')

    def status(self):
        """Returns a dictionary with the current environment status"""
        with self._lock:
            return {
                'safe': self.safe,
                'unsafe_conditions': self.unsafe_conditions,
                'updated': self.updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'conditions': {k: [(c.name, c.status) for c in v] \
                    for k, v in self._conditions.items()}
            }
