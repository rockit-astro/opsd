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

"""Script to warm and power off cameras"""

import argparse
import sys
import time
from rockit.camera.qhy import CameraStatus, CommandStatus, CoolerMode
from rockit.common import daemons, print
from rockit.operations.actions.sting.camera_helpers import cameras, das_machines


def shutdown_cameras(prefix, args):
    """warm and power off cameras"""
    parser = argparse.ArgumentParser(prefix)
    parser.add_argument('--cameras', type=str, nargs='+', choices=cameras.keys(),
                        default=cameras.keys(), help='cameras to shut down')

    args = parser.parse_args(args)

    try:
        # Disable terminal cursor
        sys.stdout.write('\033[?25l')

        sys.stdout.write('Warming cameras...')
        sys.stdout.flush()

        enabled = {}
        try:
            failed = False
            warm = {camera_id: False for camera_id in args.cameras}
            for camera_id in args.cameras:
                virt_daemon = daemons.sting_camvirt_das1 if camera_id in ['cam1', 'cam2'] else daemons.sting_camvirt_das2
                with virt_daemon.connect() as virt:
                    enabled[camera_id] = virt.report_camera_status(camera_id).get('vm_active', False)

                if enabled[camera_id]:
                    with cameras[camera_id].connect() as cam:
                        status = cam.set_target_temperature(None)
                        if status not in [CommandStatus.Succeeded, CommandStatus.CameraNotInitialized]:
                            failed = True
                            warm[camera_id] = True
                else:
                    warm[camera_id] = True

            while True:
                for camera_id in args.cameras:
                    if warm[camera_id] or not enabled[camera_id]:
                        continue

                    with cameras[camera_id].connect() as camd:
                        status = camd.report_status() or {}
                        warm[camera_id] = status.get('state', CameraStatus.Disabled) == CameraStatus.Disabled or \
                            status.get('cooler_mode', CoolerMode.Warm) == CoolerMode.Warm

                if all(warm[k] for k in warm):
                    break

                time.sleep(5)
        except Exception:
            sys.stdout.write('\r\033[K')
            print('Warming cameras [b][red]FAILED[/red][/b]')
            return

        sys.stdout.write('\r\033[K')
        if failed:
            print('Warming cameras [b][yellow]FAILED[/yellow][/b]')
        else:
            print('Warming cameras [b][green]COMPLETE[/green][/b]')

        sys.stdout.write('Shutting down cameras...')
        sys.stdout.flush()

        failed = False
        for camera_id in args.cameras:
            if not enabled[camera_id]:
                continue

            try:
                with cameras[camera_id].connect() as cam:
                    cam.shutdown()

                with daemons.sting_power.connect() as powerd:
                    p = powerd.last_measurement()
                    if camera_id in p and p[camera_id]:
                        powerd.switch(camera_id, False)
            except Exception:
                failed = True
                continue

        sys.stdout.write('\r\033[K')
        if failed:
            print('Shutting down cameras [b][red]FAILED[/red][/b]')
        else:
            print('Shutting down cameras [b][green]COMPLETE[/green][/b]')

        sys.stdout.write('Shutting down camera VMs...')
        sys.stdout.flush()

        failed = False
        for das_info in das_machines.values():
            if all(camera_id in args.cameras for camera_id in das_info['cameras']):
                try:
                    with das_info['daemon'].connect(timeout=40) as camvirtd:
                        camvirtd.shutdown()
                except Exception:
                    failed = True
                    continue

        sys.stdout.write('\r\033[K')
        if failed:
            print('Shutting down camera VMs [b][red]FAILED[/red][/b]')
        else:
            print('Shutting down camera VMs [b][green]COMPLETE[/green][/b]')

    finally:
        # Restore cursor
        sys.stdout.write('\033[?25h')
