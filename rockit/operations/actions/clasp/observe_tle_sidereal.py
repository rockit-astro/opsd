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

"""Telescope action to observe a GEO object with cam1 by allowing it to trail in front of tracked stars"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

import threading
from astropy import wcs
from astropy.coordinates import SkyCoord
from astropy.time import Time, TimeDelta
import astropy.units as u
import numpy as np
from skyfield.sgp4lib import EarthSatellite
from skyfield.api import Loader, Topos
from rockit.common import validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .mount_helpers import mount_slew_radec, mount_status, mount_offset_radec, mount_stop
from .camera_helpers import cam_take_images, cam_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import pipeline_science_schema, camera_science_schema

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = TimeDelta(25, format='sec')

# Amount of time to wait before retrying if an image acquisition generates an error
CAM_ERROR_RETRY_DELAY = TimeDelta(10, format='sec')

# Expected time to converge on target field
SETUP_DELAY = TimeDelta(15, format='sec')

# Time step to use when searching for the target leaving the field of view
FIELD_END_SEARCH_STEP = TimeDelta(5, format='sec')

# Exposure time to use when taking a WCS field image
WCS_EXPOSURE_TIME = TimeDelta(5, format='sec')


class Progress:
    Waiting, AcquiringTarget, Observing = range(3)


class ObserveTLESidereal(TelescopeAction):
    """
    Telescope action to observe a GEO object with cam1 by allowing it to trail in front of tracked stars

    Example block:
    {
        "type": "ObserveTLESidereal",
        "start": "2022-09-18T22:20:00",
        "end": "2022-09-18T22:30:00",
        "tle": [
            "0 THOR 6",
            "1 36033U 09058B   22263.78760138 -.00000015  00000-0  00000-0 0  9999",
            "2 36033   0.0158 227.3607 0002400 347.4358  67.5439  1.00272445 47312"
        ],
        "cam1": {
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, gain, offset, stream (advanced options)
        },
        "pipeline": {
           "prefix": "36033",
           "object": "THOR 6", # Optional: defaults to the TLE name without leading "0 "
           "archive": ["CAM1"] # Optional: defaults to the cameras defined in the action
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, log_name, config):
        super().__init__('Observe TLE', log_name, config)
        self._wait_condition = threading.Condition()
        self._progress = Progress.Waiting
        self._camera = 'cam1'

        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])
        self._field_end_date = None

        self._field_width = 2.6 * u.deg
        self._field_height = 1.69 * u.deg

        self._wcs_status = WCSStatus.Inactive
        self._wcs_center = None

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []

        if self._progress <= Progress.Waiting:
            if self._start_date:
                tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')
        elif not self.dome_is_open:
            tasks.append('Wait for dome')

        if self._progress <= Progress.AcquiringTarget:
            tasks.append(f'Acquire target ({self.target_name()})')
            tasks.append(f'Observe until {self._end_date.strftime("%H:%M:%S")}')
        else:
            tasks.append(f'Observe target ({self.target_name()}) until {self._field_end_date.strftime("%H:%M:%S")}')
            tasks.append(f'Reacquire and repeat until {self._end_date.strftime("%H:%M:%S")}')

        return tasks


    def __set_failed_status(self):
        """Sets self.status to Complete if aborted otherwise Error"""
        if self.aborted:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def __field_coord(self, start_time, observer, target, timescale):
        """
        Calculate the RA, Dec that places the target in the corner of the CCD
        at a given time. Returns the Astropy Time that the target leaves the opposite
        corner of the CCD

        :param start_time: Astropy Time to start tracking the object
        :returns:
            SkyCoord defining field center
            Time defining field end
        """

        start_coord = calculate_target_coord(start_time, observer, target, timescale)
        end_time = start_time
        end_coord = start_coord

        # Step forward until the target moves outside the requested footprint
        while True:
            test_time = end_time + FIELD_END_SEARCH_STEP
            if end_time > self._end_date:
                break

            test_coord = calculate_target_coord(test_time, observer, target, timescale)
            delta_ra, delta_dec = start_coord.spherical_offsets_to(test_coord)
            if np.abs(delta_ra) > self._field_width / np.cos(test_coord.dec) or np.abs(delta_dec) > self._field_height:
                break

            end_time = test_time
            end_coord = test_coord

        # Point in the middle of the start and end
        points = SkyCoord([start_coord, end_coord], unit=u.deg)
        midpoint = SkyCoord(points.data.mean(), frame=points)
        return midpoint, end_time

    def target_name(self):
        if 'object' in self.config['pipeline']:
            return self.config['pipeline']['object']

        name = self.config['tle'][0]
        if name.startswith('0 '):
            name = name[2:]
        return name

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Configure pipeline immediately so the dashboard can show target name etc
        pipeline_science_config = self.config['pipeline'].copy()
        pipeline_science_config['type'] = 'SCIENCE'
        pipeline_science_config['object'] = self.target_name()

        if 'archive' not in pipeline_science_config:
            pipeline_science_config['archive'] = [self._camera.upper()]

        # The leading line number is omitted to keep the string within the 68 character fits limit
        pipeline_science_config['headers'] = [
            {'keyword': 'TLE1', 'value': self.config['tle'][1][2:]},
            {'keyword': 'TLE2', 'value': self.config['tle'][2][2:]},
        ]

        if not configure_pipeline(self.log_name, pipeline_science_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        # Remember coordinate offset between pointings
        last_offset_ra = 0
        last_offset_dec = 0
        first_field = True

        status = mount_status(self.log_name)
        if status is None:
            self.status = TelescopeActionStatus.Error
            return

        observer = Topos(
            f'{status["site_latitude"]} N',
            f'{status["site_longitude"]} E',
            elevation_m=status['site_elevation'])

        target = EarthSatellite(
            self.config['tle'][1],
            self.config['tle'][2],
            name=self.config['tle'][0])

        timescale = Loader('/var/tmp').timescale()

        while not self.aborted and self.dome_is_open:
            self._progress = Progress.AcquiringTarget
            acquire_start = Time.now()
            if acquire_start > self._end_date:
                break

            field_start = acquire_start + SETUP_DELAY
            target_coord, field_end = self.__field_coord(field_start, observer, target, timescale)
            self._field_end_date = field_end
            if not mount_slew_radec(self.log_name,
                                    (target_coord.ra + last_offset_ra).to_value(u.deg),
                                    (target_coord.dec + last_offset_dec).to_value(u.deg),
                                    True):
                print('failed to slew to target')
                self.__set_failed_status()
                return

            # Take a frame to solve field center
            pipeline_junk_config = self.config.get('pipeline', {}).copy()
            pipeline_junk_config.update({
                'wcs': True,
                'type': 'JUNK',
                'object': 'WCS',
                'archive': []
            })

            if not configure_pipeline(self.log_name, pipeline_junk_config, quiet=True):
                self.__set_failed_status()
                return

            cam_config = {}
            cam_config.update(self.config.get(self._camera, {}))
            cam_config.update({
                'exposure': WCS_EXPOSURE_TIME.to(u.second).value,
                'stream': False
            })

            # Converge on requested position
            attempt = 1
            while not self.aborted and self.dome_is_open:
                if not cam_take_images(self.log_name, self._camera, 1, cam_config, quiet=True):
                    # Try stopping the camera, waiting a bit, then try again
                    cam_stop(self.log_name, self._camera)
                    self.wait_until_time_or_aborted(Time.now() + CAM_ERROR_RETRY_DELAY, self._wait_condition)
                    attempt += 1
                    if attempt == 6:
                        self.__set_failed_status()
                        return

                # Wait for new frame
                expected_complete = Time.now() + WCS_EXPOSURE_TIME + MAX_PROCESSING_TIME

                # TODO: Locking?
                self._wcs_status = WCSStatus.WaitingForWCS
                self._wcs_center = None

                while True:
                    with self._wait_condition:
                        remaining = (expected_complete - Time.now()).to(u.second).value
                        if remaining < 0 or self._wcs_status != WCSStatus.WaitingForWCS:
                            break

                        self._wait_condition.wait(max(remaining, 1))

                failed = self._wcs_status == WCSStatus.WCSFailed
                timeout = self._wcs_status == WCSStatus.WaitingForWCS
                self._wcs_status = WCSStatus.Inactive

                if failed or timeout:
                    if failed:
                        print('WCS failed for attempt', attempt)
                    else:
                        print('WCS timed out for attempt', attempt)

                    attempt += 1
                    if attempt == 6:
                        self.__set_failed_status()
                        return
                    continue

                # Store accumulated offset for the next frame
                offset_ra, offset_dec = self._wcs_center.spherical_offsets_to(target_coord)
                last_offset_ra += offset_ra
                last_offset_dec += offset_dec

                # Close enough!
                if abs(offset_ra) < 1 * u.arcminute and abs(offset_dec) < 1 * u.arcminute:
                    print(f'offset is {offset_ra.to_value(u.arcsecond):.1f}, {offset_dec.to_value(u.arcsecond):.1f}')
                    break

                # Offset telescope
                if not mount_offset_radec(self.log_name,
                                          offset_ra.to_value(u.deg),
                                          offset_dec.to_value(u.deg)):
                    print('failed to offset')
                    self.__set_failed_status()
                    return

            if self.aborted or not self.dome_is_open:
                break

            acquire_delay = (Time.now() - acquire_start).to(u.second).value
            print(f'Acquired field in {acquire_delay:.1f} seconds')
            print(f'Leaves field at {field_end}')

            # Start science observations
            if not configure_pipeline(self.log_name, pipeline_science_config, quiet=not first_field):
                self.__set_failed_status()
                return

            self._progress = Progress.Observing
            if not cam_take_images(self.log_name, self._camera, 0, self.config.get(self._camera, {})):
                print('Failed to take_images - will retry for next field')

            first_field = False
            # Wait until the target reaches the edge of the field of view then repeat
            # Don't bother checking for the camera timeout - this is rare
            # and we will catch it on the next field observation if it does happen
            if not self.wait_until_time_or_aborted(field_end, self._wait_condition):
                cam_stop(self.log_name, self._camera)
                print('Failed to wait until end of exposure sequence')
                self.__set_failed_status()
                return

            exposure = self.config.get(self._camera, {}).get('exposure', -1)
            cam_stop(self.log_name, self._camera, timeout=exposure + 1)

        exposure = self.config.get(self._camera, {}).get('exposure', -1)
        cam_stop(self.log_name, self._camera, timeout=exposure + 1)
        mount_stop(self.log_name)

        self.status = TelescopeActionStatus.Complete

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        with self._wait_condition:
            if self._wcs_status == WCSStatus.WaitingForWCS:
                if 'CRVAL1' in headers:
                    center_ra, center_dec = wcs.WCS(headers).all_pix2world(
                        headers['NAXIS1'] // 2, headers['NAXIS2'] // 2, 0)
                    self._wcs_center = SkyCoord(ra=center_ra, dec=center_dec, unit=u.degree, frame='icrs')
                    self._wcs_status = WCSStatus.WCSComplete
                else:
                    self._wcs_status = WCSStatus.WCSFailed

                self._wait_condition.notify_all()

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        mount_stop(self.log_name)
        cam_stop(self.log_name, self._camera)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['tle', 'start', 'end', 'pipeline', 'cam1'],
            'properties': {
                'type': {'type': 'string'},
                'tle': {
                    'type': 'array',
                    'maxItems': 3,
                    'minItems': 3,
                    'items': [
                        {
                            'type': 'string',
                        },
                        {
                            'type': 'string',
                            'minLength': 69,
                            'maxLength': 69
                        },
                        {
                            'type': 'string',
                            'minLength': 69,
                            'maxLength': 69
                        }
                    ]
                },
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'end': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'pipeline': pipeline_science_schema(),
                'cam1': camera_science_schema('cam1')
            }
        }

        schema['properties']['pipeline']['required'].remove('object')

        return validation.validation_errors(config_json, schema)


class WCSStatus:
    Inactive, WaitingForWCS, WCSFailed, WCSComplete = range(4)


def calculate_target_coord(target_time, observer, target, timescale):
    """
    Calculate the target RA and Dec at a given time
    :param time: Astropy time to evaluate
    :returns: SkyCoord with the target RA and Dec
    """
    t = timescale.from_astropy(target_time)
    ra, dec, _ = (target - observer).at(t).radec()
    return SkyCoord(ra.to(u.deg), dec.to(u.deg))
