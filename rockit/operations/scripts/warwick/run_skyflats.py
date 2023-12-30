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

"""Script to queue a skyflats action"""

import argparse
from rockit.operations.actions.warwick.camera_helpers import FOCUS_OFFSETS
from .helpers import schedule_action


def run_skyflats(prefix, args):
    """queue an automated flat field action"""
    parser = argparse.ArgumentParser(prefix)
    parser.add_argument('--morning', action='store_true',
                        help='schedule morning flats instead of evening flats')
    parser.add_argument('--filters', type=str, nargs='+', choices=FOCUS_OFFSETS.keys(),
                        help='filters to acquire flats with')
    parser.add_argument('--count', type=int, default=21,
                        help='number of images to acquire in each filter')
    parser.add_argument('--prefix', type=str, default='evening-flat',
                        help='filename prefix for saved images')

    args = parser.parse_args(args)

    action = {
        'type': 'SkyFlats',
        'evening': not args.morning,
        'pipeline': {
           'prefix': args.prefix
        }
    }

    if args.filters:
        action['filters'] = args.filters

    schedule_action(action)
