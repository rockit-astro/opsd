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

"""Script to queue a focus sweep action"""

import argparse
from rockit.operations.actions.warwick.camera_helpers import FOCUS_OFFSETS
from .helpers import schedule_action, argparse_type_ra, argparse_type_dec

def focus_pos(value):
    try:
        focus = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("focus steps must be integers")
    if focus < 0 or focus >= 100500:
        raise argparse.ArgumentTypeError(f'{focus} is outside the focuser range (0 - 100500 steps)')

    return focus

def focus_delta(value):
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("focus steps must be integers")

def run_focus_sweep(prefix, args):
    """queue an automated focus sweep action"""
    parser = argparse.ArgumentParser(prefix)
    parser.add_argument('--ra', type=argparse_type_ra, default=None,
                        help='target right ascension in h:m:s')
    parser.add_argument('--dec', type=argparse_type_dec, default=None,
                        help='target declination in d:m:s')
    parser.add_argument('--filter', type=str, choices=FOCUS_OFFSETS.keys(), default=None,
                        help='filter to use when focusing')
    parser.add_argument('--exposure', type=float, default=5,
                        help='camera exposure time')
    parser.add_argument('--samples', type=int, default=5,
                        help='number of measurements to obtain at each focus step')
    parser.add_argument('min', type=focus_pos,
                        help='minimum focus position to measure')
    parser.add_argument('max', type=focus_pos,
                        help='maximum focus position to measure')
    parser.add_argument('step', type=focus_delta,
                        help='focus steps between measurements')
    parser.add_argument('prefix', type=str,
                        help='filename prefix for the saved images')
    args = parser.parse_args(args)

    action = {
        'type': 'FocusSweep',
        'min': args.min,
        'max': args.max,
        'step': args.step,
        'samples': args.samples,
        'pipeline': {
            'prefix': args.prefix
        },
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
