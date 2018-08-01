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

"""Constants and status codes used by opsd"""

# pylint: disable=too-few-public-methods

class CommandStatus:
    """Numeric return codes"""
    # General error codes
    Succeeded = 0
    Failed = 1
    Blocked = 2
    InErrorState = 3
    InvalidControlIP = 10

    CameraActive = 11
    CoordinateSolutionFailed = 12
    TelescopeSlewFailed = 13

    InvalidSchedule = 21

    _messages = {
        # General error codes
        1: 'error: command failed',
        2: 'error: another command is already running',
        3: 'error: error state must first be cleared by switching to manual mode',
        10: 'error: command not accepted from this IP',

        11: 'error: camera is not idle',
        12: 'error: acquisition image WCS solution failed',
        13: 'error: telescope slew failed',

        21: 'error: invalid schedule definition',

        -100: 'error: terminated by user',
        -101: 'error: unable to communicate with operations daemon'
    }

    @classmethod
    def message(cls, error_code):
        """Returns a human readable string describing an error code"""
        if error_code in cls._messages:
            return cls._messages[error_code]
        return 'error: Unknown error code {}'.format(error_code)

class OperationsMode:
    """Operational status"""
    Error, Automatic, Manual = range(3)
    Names = ['Error', 'Automatic', 'Manual']
