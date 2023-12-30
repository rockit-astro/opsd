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

"""Script to queue an autofocus action"""

import argparse
from rockit.operations.actions.clasp.camera_helpers import cameras
from .helpers import schedule_action, argparse_type_ra, argparse_type_dec


def run_autofocus(prefix, args):
    """queue an automated focus action"""
    parser = argparse.ArgumentParser(prefix)
    parser.add_argument('--ra', type=argparse_type_ra, default=None,
                        help='target right ascension in h:m:s')
    parser.add_argument('--dec', type=argparse_type_dec, default=None,
                        help='target declination in d:m:s')

    parser.add_argument('--cameras', type=str, nargs='+', choices=cameras.keys(),
                        default=cameras.keys(), help='cameras to focus')
    parser.add_argument('--cam1', type=float, default=5, help='exposure time for cam1')
    parser.add_argument('--cam2', type=float, default=10, help='exposure time for cam2')
    args = parser.parse_args(args)

    action = {'type': 'AutoFocus'}

    if args.ra:
        action['ra'] = args.ra

    if args.dec:
        action['dec'] = args.dec

    if 'cam1' in args.cameras:
        action['cam1'] = {'exposure': args.cam1}

    if 'cam2' in args.cameras:
        action['cam2'] = {'exposure': args.cam2}

    schedule_action(action)
