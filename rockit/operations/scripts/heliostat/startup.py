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

"""Script to power on and initialize observatory hardware"""

import time
from rich.progress import Progress, SpinnerColumn, TextColumn
from rockit.dome.pulsar import CommandStatus as DomeCommandStatus
from rockit.common import daemons
from rockit.mount.heliostat import CommandStatus as MountCommandStatus

class Failed(Exception):
    pass


def startup(prefix, args):
    """power on and initialize instrumentation"""

    with Progress(SpinnerColumn(), TextColumn('{task.description}')) as progress:

        def task_completed(task_id):
            progress.update(task_id, total=1, completed=1,
                            description=f'{progress.tasks[task_id].description}[b][green]COMPLETE[/green][/b]')

        def task_failed(task_id):
            progress.update(task_id, total=1, completed=1,
                            description=f'{progress.tasks[task_id].description}[b][red]FAILED[/red][/b]')

        task = progress.add_task('Initializing mount...    ')

        try:
            for _ in range(3):
                with daemons.warwick_heliostat_mount.connect(timeout=20) as mount:
                    status = mount.initialize()
                    if status not in [MountCommandStatus.Succeeded, MountCommandStatus.NotDisconnected]:
                        raise Failed

            with daemons.warwick_heliostat_mount.connect(timeout=90) as mount:
                status = mount.home()
                if status not in [MountCommandStatus.Succeeded, MountCommandStatus.NotDisconnected]:
                    raise Failed

            task_completed(task)
        except Exception:
            task_failed(task)
            return

        task = progress.add_task('Homing dome...               ')

        try:
            with daemons.warwick_heliostat_dome.connect(timeout=200) as dome:
                status = dome.home_azimuth()
                if status != DomeCommandStatus.Succeeded:
                    raise Failed

                task_completed(task)
        except Exception:
            task_failed(task)
            return
