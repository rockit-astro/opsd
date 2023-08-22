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

import datetime
import threading
from rockit.common import log
from .constants import ConditionStatus


class ConditionWatcher:
    """Represents a parameter source for a condition flag"""
    def __init__(self, config):
        self.label = config['label']
        self.status = ConditionStatus.Unknown
        self._sensor = config['sensor']
        self._parameter = config['parameter']

        self._unsafe_key = 'unsafe'
        if 'unsafe_key' in config:
            self._unsafe_key = config['unsafe_key']

        self._warning_key = 'warning'
        if 'warning_key' in config:
            self._warning_key = config['warning_key']

    def update(self, data):
        """Updates the condition status based on the given environment data"""
        self.status = ConditionStatus.Unknown
        param = data.get(self._sensor, {}).get('parameters', {}).get(self._parameter, {})
        if param:
            if param[self._unsafe_key]:
                self.status = ConditionStatus.Unsafe
            elif param[self._warning_key]:
                self.status = ConditionStatus.Warning
            elif param['current']:
                self.status = ConditionStatus.Safe

    def latest(self, data):
        """Returns the latest value of the parameter, or None if it is not current"""
        param = data.get(self._sensor, {}).get('parameters', {}).get(self._parameter, {})
        if param and param['current']:
            return param['latest']

        return None


class ConditionType:
    """Represents a condition type (e.g. external humidity) with one or more sensors"""
    def __init__(self, config):
        self.label = config['label']
        self._sensors = [ConditionWatcher(sensor) for sensor in config['sensors']]

    def update(self, data):
        """
        Updates the condition status based on the given environment data.
        Returns true if this condition is safe for operations
        Returns false if any of the sensors are unsafe or if all are unknown
        """
        for s in self._sensors:
            s.update(data)

        all_unknown = all([s.status == ConditionStatus.Unknown for s in self._sensors])
        any_unsafe = any([s.status == ConditionStatus.Unsafe for s in self._sensors])
        return not (all_unknown or any_unsafe)

    def status(self):
        """
        Returns a dictionary of sensor label: condition status
        """
        return {s.label: s.status for s in self._sensors}


class EnvironmentWatcher:
    """Class that handles parsing and exposing the data from environmentd"""
    def __init__(self, config):
        self._config = config
        self._lock = threading.Lock()
        self._conditions = [ConditionType(condition) for condition in config.environment_conditions]

        self.safe = False
        self.updated = datetime.datetime.utcnow()

    def update(self):
        """Queries environmentd for new data and updates flags"""
        was_safe = self.safe
        try:
            with self._config.environment_daemon.connect() as environment:
                data = environment.status()

            safe = True
            unsafe_conditions = []
            for condition in self._conditions:
                if not condition.update(data):
                    safe = False
                    unsafe_conditions.append(condition.label)

            with self._lock:
                self.updated = datetime.datetime.utcnow()
                self.safe = safe

            if was_safe and not safe:
                unsafe_list = ', '.join(unsafe_conditions)
                log.warning(self._config.log_name, 'Environment has become unsafe (' + unsafe_list + ')')
            elif not was_safe and safe:
                log.info(self._config.log_name, 'Environment trigger timed out')
        except Exception as e:
            with self._lock:
                self.updated = datetime.datetime.utcnow()
                self.safe = False
                for condition in self._conditions:
                    condition.update({})

            log.info(self._config.log_name, 'Failed to query environment (' + str(e) + ')')

    def status(self):
        """Returns a dictionary with the current environment status"""
        with self._lock:
            return {
                'safe': self.safe,
                'updated': self.updated.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'conditions': {c.label: c.status() for c in self._conditions}
            }
