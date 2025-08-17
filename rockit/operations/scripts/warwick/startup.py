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
from rockit.dome.ash import CommandStatus as DomeCommandStatus
from rockit.focuser.atlas import CommandStatus as FocusCommandStatus
from rockit.camera.qhy import CommandStatus as CamCommandStatus
from rockit.filterwheel.fli import CommandStatus as FilterCommandStatus
from rockit.common import daemons
from rockit.mount.meade import CommandStatus as TelCommandStatus
from rockit.pipeline import CommandStatus as PipelineCommandStatus
from .helpers import power_switches


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

        task = progress.add_task('Initializing pipeline...     ')

        try:
            with daemons.warwick_pipeline.connect() as pipeline:
                status = pipeline.configure({}, quiet=True)
                if status != PipelineCommandStatus.Succeeded:
                    raise Failed

                task_completed(task)
        except Exception:
            task_failed(task)
            return

        task = progress.add_task('Initializing dome...         ')

        try:
            with daemons.warwick_dome.connect(timeout=10) as dome:
                status = dome.initialize()
                if status not in [DomeCommandStatus.Succeeded, DomeCommandStatus.NotDisconnected]:
                    raise Failed

                task_completed(task)
        except Exception:
            task_failed(task)
            return

        task = progress.add_task('Initializing power...        ')
        switched = False
        try:
            with daemons.warwick_power.connect() as power:
                status = power.last_measurement()
                for p in power_switches:
                    if not status.get(p, True):
                        power.switch(p, True)
                        switched = True
        except Exception:
            task_failed(task)
            return

        # Wait for cameras to power up
        if switched:
            time.sleep(5)

        task_completed(task)

        task = progress.add_task('Initializing camera...       ')

        try:
            with daemons.warwick_camera.connect(timeout=10) as cam:
                status = cam.initialize()
                if status not in [CamCommandStatus.Succeeded, CamCommandStatus.CameraNotUninitialized]:
                    raise Failed

                if cam.configure({}, quiet=True) != CamCommandStatus.Succeeded:
                    raise Failed

                task_completed(task)
        except Exception:
            task_failed(task)
            return

        task = progress.add_task('Initializing filter wheel... ')

        try:
            with daemons.warwick_filterwheel.connect(timeout=15) as filt:
                status = filt.initialize()
                if status not in [FilterCommandStatus.Succeeded, FilterCommandStatus.NotDisconnected]:
                    raise Failed

                task_completed(task)
        except Exception:
            task_failed(task)
            return

        task = progress.add_task('Initializing focuser...      ')

        try:
            with daemons.warwick_focuser.connect(timeout=15) as focuser:
                status = focuser.initialize()
                if status not in [FocusCommandStatus.Succeeded, FocusCommandStatus.NotDisconnected]:
                    raise Failed

                task_completed(task)
        except Exception:
            task_failed(task)
            return

        task = progress.add_task('Initializing telescope...    ')

        try:
            with daemons.warwick_telescope.connect(timeout=125) as telescope:
                status = telescope.initialize()
                if status not in [TelCommandStatus.Succeeded, TelCommandStatus.NotDisconnected]:
                    raise Failed

                task_completed(task)
        except Exception:
            task_failed(task)
            return

        task = progress.add_task('Homing dome...               ')

        try:
            with daemons.warwick_dome.connect(timeout=200) as dome:
                status = dome.home_azimuth()
                if status != DomeCommandStatus.Succeeded:
                    raise Failed

                task_completed(task)
        except Exception:
            task_failed(task)
            return
