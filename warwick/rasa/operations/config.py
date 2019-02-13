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

"""opsd RASA-specific definitions"""

from warwick.observatory.common import daemons, IP
from warwick.observatory.operations import ConditionWatcher
from .actions import Initialize, Shutdown

from .actions import (
    AutoFocus,
    FocusSweep,
    ImageSet,
    OffsetTelescope,
    SlewTelescope,
    SlewTelescopeAltAz,
    SkyFlats,
    Wait,
    WaitForDome,
    WaitUntil)

class RASAConfig:
    """Configuration for the RASA prototype"""
    # Machines that are allowed to issue commands
    control_ips = [IP.RASAMain]

    # Machines that are allowed to notify processed frames
    processed_frame_ips = [IP.RASAMain]

    # Communications timeout when opening or closing the dome (takes up to ~80 seconds)
    dome_pyro_openclose_timeout = 120

    # Timeout period (seconds) for the dome controller
    # The dome heartbeat is pinged once per LOOP_DELAY when the dome is under
    # automatic control and is fully open or fully closed.  This timeout should
    # be large enough to account for the time it takes to open and close the dome
    dome_heartbeat_timeout = 119

    ops_daemon = daemons.rasa_operations
    dome_daemon = daemons.rasa_dome
    environment_daemon = daemons.rasa_environment
    power_daemon = daemons.rasa_power
    log_name = 'rasa_opsd'
    telescope_initialize_action = Initialize
    telescope_shutdown_action = Shutdown

    def get_enviroment_conditions():
        return [
            # Wind
            ConditionWatcher('wind', 'vaisala', 'wind_speed', 'W1m'),
            ConditionWatcher('wind', 'goto_vaisala', 'wind_speed', 'GOTO'),
            ConditionWatcher('wind', 'superwasp', 'wind_speed', 'SWASP'),
            ConditionWatcher('median_wind', 'vaisala', 'median_wind_speed', 'W1m'),
            ConditionWatcher('median_wind', 'goto_vaisala', 'median_wind_speed', 'GOTO'),
            ConditionWatcher('median_wind', 'superwasp', 'median_wind_speed', 'SWASP'),

            # Temperature
            ConditionWatcher('temperature', 'vaisala', 'temperature', 'W1m'),
            ConditionWatcher('temperature', 'goto_vaisala', 'temperature', 'GOTO'),
            ConditionWatcher('temperature', 'superwasp', 'ext_temperature', 'SWASP'),

            # Humidity
            ConditionWatcher('humidity', 'vaisala', 'relative_humidity', 'W1m'),
            ConditionWatcher('humidity', 'goto_vaisala', 'relative_humidity', 'GOTO'),
            ConditionWatcher('humidity', 'superwasp', 'ext_humidity', 'SWASP'),
            ConditionWatcher('internal_humidity', 'goto_roomalert',
                             'dome2_internal_humidity', 'RASA'),

            # Dew point
            ConditionWatcher('dewpt', 'vaisala', 'dew_point_delta', 'W1m'),
            ConditionWatcher('dewpt', 'goto_vaisala', 'dew_point_delta', 'GOTO'),
            ConditionWatcher('dewpt', 'superwasp', 'dew_point_delta', 'SWASP'),

            # Rain detectors
            ConditionWatcher('rain', 'vaisala', 'accumulated_rain', 'W1m'),
            ConditionWatcher('rain', 'goto_vaisala', 'accumulated_rain', 'GOTO'),

            # Network
            ConditionWatcher('netping', 'netping', 'google', 'Google'),
            ConditionWatcher('netping', 'netping', 'ngtshead', 'NGTSHead'),

            # Power
            ConditionWatcher('ups', 'power', 'ups_status', 'Status'),
            ConditionWatcher('ups', 'power', 'ups_battery_remaining', 'Battery'),

            # Disk space
            ConditionWatcher('diskspace', 'diskspace', 'data_fs_available_bytes', 'Bytes'),

            # Sun altitude
            ConditionWatcher('sun', 'ephem', 'sun_alt', 'Altitude')
        ]

    def get_action_types():
        return {
            'AutoFocus': AutoFocus,
            'FocusSweep': FocusSweep,
            'SkyFlats': SkyFlats,
            'ImageSet': ImageSet,
            'OffsetTelescope': OffsetTelescope,
            'SlewTelescope': SlewTelescope,
            'SlewTelescopeAltAz': SlewTelescopeAltAz,
            'Wait': Wait,
            'WaitForDome': WaitForDome,
            'WaitUntil': WaitUntil
        }
