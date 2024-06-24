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

"""Constants and status codes used by opsd"""


class CommandStatus:
    """Numeric return codes"""
    # General error codes
    Succeeded = 0
    Failed = 1
    Blocked = 2
    InErrorState = 3
    InvalidControlIP = 10

    CameraActive = 11
    TelescopeSlewFailed = 13

    InvalidSchedule = 21
    DomeNotAutomatic = 22
    DomeNotClosed = 23
    TelescopeNotAutomatic = 24
    EnvironmentNotSafe = 25

    _messages = {
        # General error codes
        1: 'error: command failed',
        2: 'error: another command is already running',
        3: 'error: error state must first be cleared by switching to manual mode',
        10: 'error: command not accepted from this IP',

        11: 'error: camera is not idle',
        13: 'error: telescope slew failed',

        21: 'error: invalid schedule definition',
        22: 'error: dome is not in automatic mode',
        23: 'error: dome is not closed',
        24: 'error: telescope is not in automatic mode',
        25: 'error: environment is not safe',

        -100: 'error: terminated by user',
        -101: 'error: unable to communicate with operations daemon'
    }

    @classmethod
    def message(cls, error_code):
        """Returns a human readable string describing an error code"""
        if error_code in cls._messages:
            return cls._messages[error_code]
        return f'error: Unknown error code {error_code}'


class OperationsMode:
    """Operational status"""
    Error, Automatic, Manual = range(3)

    _labels = {
        0: 'ERROR',
        1: 'AUTOMATIC',
        2: 'MANUAL'
    }

    _colors = {
        0: 'red',
        1: 'green',
        2: 'yellow'
    }

    @classmethod
    def label(cls, status, formatting=False):
        """
        Returns a human readable string describing a status
        Set formatting=true to enable terminal formatting characters
        """
        if formatting:
            if status in cls._labels and status in cls._colors:
                return f'[b][{cls._colors[status]}]{cls._labels[status]}[/{cls._colors[status]}][/b]'
            return '[b][red]UNKNOWN[/red][/b]'

        if status in cls._labels:
            return cls._labels[status]
        return 'UNKNOWN'


class DomeStatus:
    """Aggregated dome status"""
    Closed, Open, Moving, Timeout = range(4)

    _labels = {
        0: 'CLOSED',
        1: 'OPEN',
        2: 'MOVING',
        3: 'TIMEOUT'
    }

    _colors = {
        0: 'red',
        1: 'green',
        2: 'yellow',
        3: 'red'
    }

    @classmethod
    def label(cls, status, formatting=False):
        """
        Returns a human readable string describing a status
        Set formatting=true to enable terminal formatting characters
        """
        if formatting:
            if status in cls._labels and status in cls._colors:
                return f'[b][{cls._colors[status]}]{cls._labels[status]}[/{cls._colors[status]}][/b]'
            return '[b][red]UNKNOWN[/red][/b]'

        if status in cls._labels:
            return cls._labels[status]
        return 'UNKNOWN'


class ConditionStatus:
    """Represents the status of a condition type"""
    Unknown, Safe, Warning, Unsafe = range(4)

    _colors = {
        0: 'cyan',
        1: 'green',
        2: 'yellow',
        3: 'red',
    }

    @classmethod
    def format_label(cls, status, label):
        if status in cls._colors:
            return f'[b][{cls._colors[status]}]{label}[/{cls._colors[status]}][/b]'
        return label
