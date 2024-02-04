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

"""Script to power on and initialize cameras"""

import argparse
import sys
import time
from rockit.camera.qhy import CommandStatus
from rockit.common import daemons, TFmt
from rockit.operations.actions.superwasp.camera_helpers import cameras, das_machines


def initialize_cameras(prefix, args):
    """power on and initialize cameras"""
    parser = argparse.ArgumentParser(prefix)
    parser.add_argument('--cameras', type=str, nargs='+', choices=cameras.keys(),
                        default=cameras.keys(), help='cameras to initialize')

    args = parser.parse_args(args)

    try:
        # Disable terminal cursor
        sys.stdout.write('\033[?25l')

        sys.stdout.write('Initializing camera VMs...')
        sys.stdout.flush()

        failed = False
        for das_info in das_machines.values():
            if any(camera_id in args.cameras for camera_id in das_info['cameras']):
                try:
                    with das_info['daemon'].connect(timeout=70) as camvirtd:
                        camvirtd.initialize()
                except Exception:
                    failed = True
                    continue

        if failed:
            print(f'\r\033[KInitializing camera VMs {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')
            return
        else:
            print(f'\r\033[KInitializing camera VMs {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')

        sys.stdout.write('Powering on cameras...')
        sys.stdout.flush()
        switched = False
        try:
            with daemons.superwasp_power.connect() as powerd:
                p = powerd.last_measurement()
                for camera_id in args.cameras:
                    if camera_id in p and not p[camera_id]:
                        switched = True
                        powerd.switch(camera_id, True)
        except Exception:
            print(f'\r\033[KPowering on cameras {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')
            return

        # Wait for cameras to power up
        if switched:
            time.sleep(5)

        print(f'\r\033[KPowering on cameras {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')

        class Failed(Exception):
            pass

        for camera_id in args.cameras:
            sys.stdout.write(f'Initializing {camera_id}...')
            sys.stdout.flush()

            try:
                with cameras[camera_id].connect(timeout=10) as cam:
                    status = cam.initialize()
                    if status not in [CommandStatus.Succeeded, CommandStatus.CameraNotUninitialized]:
                        raise Failed

                    if cam.configure({}, quiet=True) != CommandStatus.Succeeded:
                        raise Failed

                    print(f'\r\033[KInitializing {camera_id} {TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')
            except Exception:
                print(f'\r\033[KInitializing {camera_id} {TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')

    finally:
        # Restore cursor
        sys.stdout.write('\033[?25h')
