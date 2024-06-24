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

import sys
import time
from rockit.ashdome import CommandStatus as DomeCommandStatus
from rockit.atlas import CommandStatus as FocusCommandStatus
from rockit.camera.qhy import CommandStatus as CamCommandStatus
from rockit.cfw import CommandStatus as FilterCommandStatus
from rockit.common import daemons, print
from rockit.meade import CommandStatus as TelCommandStatus
from rockit.pipeline import CommandStatus as PipelineCommandStatus
from .helpers import power_switches


class Failed(Exception):
    pass


def startup(prefix, args):
    """power on and initialize instrumentation"""

    try:
        # Disable terminal cursor
        sys.stdout.write('\033[?25l')

        sys.stdout.write('Initializing pipeline...')
        sys.stdout.flush()

        try:
            with daemons.warwick_pipeline.connect() as pipeline:
                status = pipeline.configure({}, quiet=True)
                if status != PipelineCommandStatus.Succeeded:
                    raise Failed

                print(f'\r\033[KInitializing pipeline...     [b][green]COMPLETE[/green][/b]')
        except Exception:
            print(f'\r\033[KInitializing pipeline...     [b][red]FAILED[/red][/b]')
            return

        sys.stdout.write('Initializing dome...')
        sys.stdout.flush()

        try:
            with daemons.warwick_dome.connect(timeout=10) as dome:
                status = dome.initialize()
                if status not in [DomeCommandStatus.Succeeded, DomeCommandStatus.NotDisconnected]:
                    raise Failed

                print(f'\r\033[KInitializing dome...         [b][green]COMPLETE[/green][/b]')
        except Exception:
            print(f'\r\033[KInitializing dome...         [b][red]FAILED[/red][/b]')
            return

        sys.stdout.write('Initializing power...')
        sys.stdout.flush()
        switched = False
        try:
            with daemons.warwick_power.connect() as power:
                status = power.last_measurement()
                for p in power_switches:
                    if not status.get(p, True):
                        power.switch(p, True)
                        switched = True
        except Exception:
            print(f'\r\033[KInitializing power...        [b][red]FAILED[/red][/b]')
            return

        # Wait for cameras to power up
        if switched:
            time.sleep(5)

        print(f'\r\033[KInitializing power...        [b][green]COMPLETE[/green][/b]')

        sys.stdout.write('Initializing camera...')
        sys.stdout.flush()

        try:
            with daemons.warwick_camera.connect(timeout=10) as cam:
                status = cam.initialize()
                if status not in [CamCommandStatus.Succeeded, CamCommandStatus.CameraNotUninitialized]:
                    raise Failed

                if cam.configure({}, quiet=True) != CamCommandStatus.Succeeded:
                    raise Failed

                print(f'\r\033[KInitializing camera...       [b][green]COMPLETE[/green][/b]')
        except Exception:
            print(f'\r\033[KInitializing camera...       [b][red]FAILED[/red][/b]')
            return

        sys.stdout.write('Initializing filter wheel...')
        sys.stdout.flush()

        try:
            with daemons.warwick_filterwheel.connect(timeout=15) as filt:
                status = filt.initialize()
                if status not in [FilterCommandStatus.Succeeded, FilterCommandStatus.NotDisconnected]:
                    raise Failed

                print(f'\r\033[KInitializing filter wheel... [b][green]COMPLETE[/green][/b]')
        except Exception:
            print(f'\r\033[KInitializing filter wheel... [b][red]FAILED[/red][/b]')
            return

        sys.stdout.write('Initializing focuser...')
        sys.stdout.flush()

        try:
            with daemons.warwick_focuser.connect(timeout=15) as focuser:
                status = focuser.initialize()
                if status not in [FocusCommandStatus.Succeeded, FocusCommandStatus.NotDisconnected]:
                    raise Failed

                print(f'\r\033[KInitializing focuser...      [b][green]COMPLETE[/green][/b]')
        except Exception:
            print(f'\r\033[KInitializing focuser...      [b][red]FAILED[/red][/b]')
            return

        sys.stdout.write('Initializing telescope...')
        sys.stdout.flush()

        try:
            with daemons.warwick_telescope.connect(timeout=125) as telescope:
                status = telescope.initialize()
                if status not in [TelCommandStatus.Succeeded, TelCommandStatus.NotDisconnected]:
                    raise Failed

                print(f'\r\033[KInitializing telescope...    [b][green]COMPLETE[/green][/b]')
        except Exception:
            print(f'\r\033[KInitializing telescope...    [b][red]FAILED[/red][/b]')
            return

        sys.stdout.write('Homing dome...')
        sys.stdout.flush()

        try:
            with daemons.warwick_dome.connect(timeout=200) as dome:
                status = dome.home_azimuth()
                if status != DomeCommandStatus.Succeeded:
                    raise Failed

                print(f'\r\033[KHoming dome...               [b][green]COMPLETE[/green][/b]')
        except Exception:
            print(f'\r\033[KHoming dome...               [b][red]FAILED[/red][/b]')
            return

    finally:
        # Restore cursor
        sys.stdout.write('\033[?25h')
