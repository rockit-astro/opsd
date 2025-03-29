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

"""Helper functions for coordinate calculations"""


from skyfield.api import load, load_file

def sun_altaz(site_location):
    """Calculate the current Alt and Az of the Sun, in degrees"""
    t = load.timescale().now()
    eph = load_file('/etc/opsd/de421.bsp')
    alt, az, _ = (eph['earth'] + site_location).at(t).observe(eph['sun']).apparent().altaz()
    return alt.degrees, az.degrees
