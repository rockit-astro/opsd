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

"""Client-side scripts that can be run for semi-automated behaviours"""

import argparse
import Pyro4
from astropy.coordinates import Latitude, Longitude
from astropy.time import Time
import astropy.units as u
from rockit.common import daemons
from rockit.operations import CommandStatus


def schedule_action(action):
    night = Time.now()
    if night.to_datetime().hour < 12:
        night -= 1 * u.day

    schedule = {
        'night': night.strftime('%Y-%m-%d'),
        'actions': [action]
    }

    try:
        with daemons.portable_operations.connect() as ops:
            ret = ops.schedule_observations(schedule)
    except Pyro4.errors.CommunicationError:
        ret = -101

    if ret != CommandStatus.Succeeded:
        print(CommandStatus.message(ret))
    return ret


def argparse_type_ra(arg):
    """Custom argparse type specifying a RA value"""
    try:
        return Longitude(arg, unit=u.hourangle).to_value(u.deg)
    except:
        raise argparse.ArgumentTypeError(f'invalid value \'{arg}\'')


def argparse_type_dec(arg):
    """Custom argparse type specifying a Dec value"""
    try:
        return Latitude(arg, unit=u.deg).to_value(u.deg)
    except:
        raise argparse.ArgumentTypeError(f'invalid value \'{arg}\'')
