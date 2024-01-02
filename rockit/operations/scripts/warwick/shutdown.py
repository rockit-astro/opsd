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

"""Script to shut down and power off observatory hardware"""

import sys
import time
from rockit.ashdome import CommandStatus as DomeCommandStatus
from rockit.atlas import CommandStatus as FocusCommandStatus
from rockit.camera.qhy import CommandStatus as CamCommandStatus, CameraStatus, CoolerMode
from rockit.cfw import CommandStatus as FilterCommandStatus
from rockit.common import daemons, TFmt
from rockit.meade import CommandStatus as TelCommandStatus, TelescopeState
from rockit.operations import DomeStatus, OperationsMode
from .helpers import power_switches


class Failed(Exception):
    pass


def shutdown(prefix, args):
    """shut down and power off instrumentation"""

    try:
        # Disable terminal cursor
        sys.stdout.write('\033[?25l')
        sys.stdout.write('Stopping operations...')
        sys.stdout.flush()

        try:
            with daemons.warwick_operations.connect() as ops:
                status = ops.status()
                if status.get('dome', {}).get('requested_close_date', None):
                    ops.clear_dome_window()

                if status.get('telescope', {}).get('schedule', []):
                    ops.stop_telescope()

                    # Wait for action to abort
                    while True:
                        status = ops.status()
                        if not status.get('telescope', {}).get('schedule', []):
                            break
                        time.sleep(5)

                # Wait for dome to close
                while True:
                    status = ops.status()
                    if status.get('dome', {}).get('status', DomeStatus.Open) == DomeStatus.Closed:
                        break
                    time.sleep(5)

                if status.get('telescope', {}).get('mode', OperationsMode.Error) != OperationsMode.Manual:
                    ops.tel_control(False)
                print(f'\r\033[KStopping operations...        {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')
        except Exception:
            print(f'\r\033[KStopping operations...        {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')
            return

        sys.stdout.write('Warming camera...')
        sys.stdout.flush()
        try:
            warm = False
            with daemons.warwick_camera.connect() as cam:
                status = cam.set_target_temperature(None)
                if status != CamCommandStatus.Succeeded:
                    warm = True

            while not warm:
                with daemons.warwick_camera.connect() as cam:
                    status = cam.report_status() or {}
                    if 'state' not in status or 'cooler_mode' not in status:
                        warm = True
                    else:
                        warm = status['state'] == CameraStatus.Disabled or \
                               status['cooler_mode'] == CoolerMode.Warm
        except Exception:
            print(f'\r\033[KWarming camera...             {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')
            return

        print(f'\r\033[KWarming camera...             {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')

        sys.stdout.write('Shutting down camera...')
        sys.stdout.flush()
        try:
            with daemons.warwick_camera.connect() as cam:
                status = cam.shutdown()
                if status not in [CamCommandStatus.Succeeded, CamCommandStatus.CameraNotInitialized]:
                    raise Failed

                print(f'\r\033[KShutting down camera...       {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')
        except Exception:
            print(f'\r\033[KShutting down camera...       {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')
            return

        sys.stdout.write('Shutting down filter wheel...')
        sys.stdout.flush()
        try:
            with daemons.warwick_filterwheel.connect() as filt:
                status = filt.shutdown()
                if status not in [FilterCommandStatus.Succeeded, FilterCommandStatus.NotConnected]:
                    raise Failed

                print(f'\r\033[KShutting down filter wheel... {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')
        except Exception:
            print(f'\r\033[KShutting down filter wheel... {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')
            return

        sys.stdout.write('Shutting down focuser...')
        sys.stdout.flush()
        try:
            with daemons.warwick_focuser.connect(timeout=15) as focuser:
                status = focuser.shutdown()
                if status not in [FocusCommandStatus.Succeeded, FocusCommandStatus.NotConnected]:
                    raise Failed

                print(f'\r\033[KShutting down focuser...      {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')
        except Exception:
            print(f'\r\033[KShutting down focuser...      {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')

        sys.stdout.write('Shutting down telescope...')
        sys.stdout.flush()
        try:
            with daemons.warwick_telescope.connect(timeout=120) as telescope:
                status = telescope.report_status() or {}

            if status.get('state', TelescopeState.Disabled) != TelescopeState.Disabled:
                with daemons.warwick_telescope.connect(timeout=120) as telescope:
                    status = telescope.park('stow')
                    if status not in [TelCommandStatus.Succeeded, TelCommandStatus.NotConnected]:
                        raise Failed

                    status = telescope.shutdown()
                    if status not in [TelCommandStatus.Succeeded, TelCommandStatus.NotConnected]:
                        raise Failed

                # Wait for the mount to store its position (and make an audible beep) before powering off!
                time.sleep(30)
                print(f'\r\033[KShutting down telescope...    {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')
        except Exception:
            print(f'\r\033[KShutting down telescope...    {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')
            return

        sys.stdout.write('Shutting down dome...')
        sys.stdout.flush()
        try:
            with daemons.warwick_operations.connect() as ops:
                status = ops.status()
                if status.get('dome', {}).get('mode', OperationsMode.Error) != OperationsMode.Manual:
                    ops.dome_control(False)

            with daemons.warwick_dome.connect() as dome:
                status = dome.shutdown()
                if status not in [DomeCommandStatus.Succeeded, DomeCommandStatus.NotConnected]:
                    raise Failed

            print(f'\r\033[KShutting down dome...         {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')
        except Exception:
            print(f'\r\033[KShutting down dome...         {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')
            return

        sys.stdout.write('Shutting down power...')
        sys.stdout.flush()
        try:
            with daemons.warwick_power.connect() as power:
                status = power.last_measurement()
                for p in power_switches:
                    if status.get(p, False):
                        power.switch(p, False)
        except Exception:
            print(f'\r\033[KShutting down power...        {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')
            return

        print(f'\r\033[KShutting down power...        {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')

    finally:
        # Restore cursor
        sys.stdout.write('\033[?25h')
