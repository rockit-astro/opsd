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


def zenith_radec(site_location):
    """Calculate the current RA and Dec of the zenith, in degrees"""
    t = load.timescale().now()
    ra, dec, _ = site_location.at(t).from_altaz(alt_degrees=90.0, az_degrees=0.0).radec()
    return ra._degrees, dec.degrees


def sun_altaz(site_location):
    """Calculate the current Alt and Az of the Sun, in degrees"""
    t = load.timescale().now()
    eph = load_file('/etc/opsd/de421.bsp')
    alt, az, _ = (eph['earth'] + site_location).at(t).observe(eph['sun']).apparent().altaz()
    return alt.degrees, az.degrees


def altaz_to_radec(site_location, alt_degrees, az_degrees):
    t = load.timescale().now()
    earth = load_file('/etc/opsd/de421.bsp')['earth']
    ra, dec, _ = (earth + site_location).at(t).from_altaz(alt_degrees=alt_degrees, az_degrees=az_degrees).radec()
    return ra._degrees, dec.degrees
