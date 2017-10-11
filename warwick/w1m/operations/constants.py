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

    CameraActive = 11

    CoordinateSolutionFailed = 12
    TelescopeSlewFailed = 13

class OperationsMode:
    """Operational status"""
    Error, Automatic, Manual = range(3)
    Names = ['Error', 'Automatic', 'Manual']

class DehumidifierMode:
    """Dehumidifier control status"""
    Manual, Automatic = range(2)
    Names = ['Manual', 'Automatic']
