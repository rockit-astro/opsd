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

from datetime import datetime
import time
from rich.progress import Progress, SpinnerColumn, TextColumn
from rockit.dome.ash import CommandStatus as DomeCommandStatus
from rockit.focuser.atlas import CommandStatus as FocusCommandStatus
from rockit.camera.qhy import CommandStatus as CamCommandStatus, CameraStatus, CoolerMode
from rockit.filterwheel.fli import CommandStatus as FilterCommandStatus
from rockit.common import daemons
from rockit.mount.meade import CommandStatus as TelCommandStatus, TelescopeState
from rockit.operations import DomeStatus, OperationsMode
from .helpers import power_switches

CAMERA_WARMING_TIMEOUT = 300


class Failed(Exception):
    pass


def shutdown(prefix, args):
    """shut down and power off instrumentation"""

    with Progress(SpinnerColumn(), TextColumn('{task.description}')) as progress:

        def task_completed(task_id):
            progress.update(task_id, total=1, completed=1,
                            description=f'{progress.tasks[task_id].description}[b][green]COMPLETE[/green][/b]')

        def task_failed(task_id):
            progress.update(task_id, total=1, completed=1,
                            description=f'{progress.tasks[task_id].description}[b][red]FAILED[/red][/b]')

        task = progress.add_task('Stopping operations...        ')

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
                if status.get('dome', {}).get('mode', OperationsMode.Error) == OperationsMode.Automatic:
                    while True:
                        status = ops.status()
                        if status.get('dome', {}).get('status', DomeStatus.Closed) == DomeStatus.Closed:
                            break
                        time.sleep(5)

                if status.get('telescope', {}).get('mode', OperationsMode.Error) != OperationsMode.Manual:
                    ops.tel_control(False)

                task_completed(task)
        except Exception:
            task_failed(task)

        task = progress.add_task('Warming camera...             ')
        warm = False

        try:
            with daemons.warwick_camera.connect() as cam:
                status = cam.set_target_temperature(None)
                if status != CamCommandStatus.Succeeded:
                    warm = True
        except Exception:
            pass

        start = datetime.now()
        while not warm:
            time.sleep(5)
            if (datetime.now() - start).total_seconds() > CAMERA_WARMING_TIMEOUT:
                break

            try:
                with daemons.warwick_camera.connect() as cam:
                    status = cam.report_status() or {}
                    if 'state' not in status or 'cooler_mode' not in status:
                        warm = True
                    else:
                        warm = status['state'] == CameraStatus.Disabled or \
                               status['cooler_mode'] == CoolerMode.Warm
            except Exception:
                pass
        if warm:
            task_completed(task)
        else:
            task_failed(task)

        task = progress.add_task('Shutting down camera...       ')
        try:
            with daemons.warwick_camera.connect() as cam:
                status = cam.shutdown()
                if status not in [CamCommandStatus.Succeeded, CamCommandStatus.CameraNotInitialized]:
                    raise Failed
                task_completed(task)
        except Exception:
            task_failed(task)

        task = progress.add_task('Shutting down filter wheel... ')
        try:
            with daemons.warwick_filterwheel.connect() as filt:
                status = filt.shutdown()
                if status not in [FilterCommandStatus.Succeeded, FilterCommandStatus.NotConnected]:
                    raise Failed

                task_completed(task)
        except Exception:
            task_failed(task)

        task = progress.add_task('Shutting down focuser...      ')
        try:
            with daemons.warwick_focuser.connect(timeout=15) as focuser:
                status = focuser.shutdown()
                if status not in [FocusCommandStatus.Succeeded, FocusCommandStatus.NotConnected]:
                    raise Failed

                task_completed(task)
        except Exception:
            task_failed(task)

        task = progress.add_task('Shutting down telescope...    ')
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

            task_completed(task)
        except Exception:
            task_failed(task)

        task = progress.add_task('Shutting down dome...         ')
        try:
            with daemons.warwick_operations.connect() as ops:
                status = ops.status()
                if status.get('dome', {}).get('mode', OperationsMode.Error) != OperationsMode.Manual:
                    ops.dome_control(False)

            with daemons.warwick_dome.connect() as dome:
                status = dome.shutdown()
                if status not in [DomeCommandStatus.Succeeded, DomeCommandStatus.NotConnected]:
                    raise Failed

            task_completed(task)
        except Exception:
            task_failed(task)

        task = progress.add_task('Shutting down power...        ')
        try:
            with daemons.warwick_power.connect() as power:
                status = power.last_measurement()
                for p in power_switches:
                    if status.get(p, False):
                        power.switch(p, False)

            task_completed(task)
        except Exception:
            task_failed(task)
