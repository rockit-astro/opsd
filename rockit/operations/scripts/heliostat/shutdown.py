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
from rockit.dome.pulsar import CommandStatus as DomeCommandStatus
from rockit.common import daemons
from rockit.mount.heliostat import CommandStatus as MountCommandStatus, MountStatus
from rockit.operations import DomeStatus, OperationsMode


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
            with daemons.warwick_heliostat_operations.connect() as ops:
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

        task = progress.add_task('Shutting down mount...    ')
        try:
            with daemons.warwick_heliostat_mount.connect() as mount:
                status = mount.report_status() or {}

            if status.get('state', MountStatus.Disabled) != MountStatus.Disabled:
                with daemons.warwick_heliostat_mount.connect(timeout=120) as mount:
                    status = mount.park('stow')
                    if status not in [MountCommandStatus.Succeeded, MountCommandStatus.NotConnected]:
                        raise Failed

                    status = mount.shutdown()
                    if status not in [MountCommandStatus.Succeeded, MountCommandStatus.NotConnected]:
                        raise Failed

            task_completed(task)
        except Exception:
            task_failed(task)

        task = progress.add_task('Shutting down dome...         ')
        try:
            with daemons.warwick_heliostat_operations.connect() as ops:
                status = ops.status()
                if status.get('dome', {}).get('mode', OperationsMode.Error) != OperationsMode.Manual:
                    ops.dome_control(False)

            task_completed(task)
        except Exception:
            task_failed(task)
