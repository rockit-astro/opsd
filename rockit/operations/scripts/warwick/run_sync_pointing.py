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

"""queue an automated pointing sync"""

import argparse
from rockit.operations.actions.warwick.camera_helpers import FOCUS_OFFSETS
from .helpers import schedule_action, argparse_type_ra, argparse_type_dec


def run_sync_pointing(prefix, args):
    """queue an automated pointing sync"""
    parser = argparse.ArgumentParser(prefix)
    parser.add_argument('--ra', type=argparse_type_ra, default=None,
                        help='target right ascension in h:m:s')
    parser.add_argument('--dec', type=argparse_type_dec, default=None,
                        help='target declination in d:m:s')
    parser.add_argument('--filter', type=str, choices=FOCUS_OFFSETS.keys(), default=None,
                        help='filter to use when syncing')
    parser.add_argument('--exposure', type=float, default=5,
                        help='camera exposure time')
    args = parser.parse_args(args)

    action = {
        'type': 'SyncPointing',
        'camera': {
            'exposure': args.exposure
        }
    }

    if args.ra:
        action['ra'] = args.ra

    if args.dec:
        action['dec'] = args.dec

    if args.filter:
        action['camera']['filter'] = args.filter

    schedule_action(action)
