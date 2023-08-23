#
# This file is part of opsd.
#
# opsd is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# opsd is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with opsd.  If not, see <http://www.gnu.org/licenses/>.

"""Telescope action to observe a target defined by a Two Line Element orbit"""

# pylint: disable=too-many-branches

import threading
from astropy.time import Time
import astropy.units as u
from skyfield.sgp4lib import EarthSatellite
from skyfield.api import Loader, Topos
from rockit.common import validation
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from .mount_helpers import mount_track_tle, mount_stop, mount_status
from .camera_helpers import cameras, cam_take_images, cam_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import pipeline_science_schema, camera_science_schema

LOOP_INTERVAL = 5
MIN_ALTITUDE = 10


class ObserveTLETracking(TelescopeAction):
    """
    Telescope action to observe a satellite tracking its TLE

    Example block:
    {
        "type": "ObserveTLETracking",
        "start": "2022-09-18T22:20:00",
        "end": "2022-09-18T22:30:00",
        "onsky": true, # Optional: defaults to true
        "tle": [
            "0 EGS (AJISAI)",
            "1 16908U 86061A   22263.84101197 -.00000106  00000-0 -62449-4 0  9999",
            "2 16908  50.0120  25.7334 0011166 305.3244 183.4486 12.44497094310376"
        ],
        "cam1": { # Optional: cameras that aren't listed won't be focused
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, window, gain, offset, stream (advanced options)
        },
        "cam2": { # Optional: cameras that aren't listed won't be focused
            "exposure": 1,
            # Also supports optional temperature (advanced options)
        },
        "pipeline": {
           "prefix": "16908",
           "object": "EGS (AJISAI)", # Optional: defaults to the TLE name without leading "0 "
           "archive": ["CAM1"] # Optional: defaults to the cameras defined in the action
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, log_name, config):
        super().__init__('Observe TLE', log_name, config)
        self._wait_condition = threading.Condition()

        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])

        self._camera_ids = [c for c in cameras if c in self.config]

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Configure pipeline immediately so the dashboard can show target name etc
        pipeline_science_config = self.config['pipeline'].copy()
        pipeline_science_config['type'] = 'SCIENCE'
        if 'object' not in pipeline_science_config:
            name = self.config['tle'][0]
            if name.startswith('0 '):
                name = name[2:]
            pipeline_science_config['object'] = name

        if 'archive' not in pipeline_science_config:
            pipeline_science_config['archive'] = [camera_id.upper() for camera_id in self._camera_ids]

        # The leading line number is omitted to keep the string within the 68 character fits limit
        pipeline_science_config['headers'] = [
            {'keyword': 'TLE1', 'value': self.config['tle'][1][2:]},
            {'keyword': 'TLE2', 'value': self.config['tle'][2][2:]},
        ]

        if not configure_pipeline(self.log_name, pipeline_science_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        self.set_task('Waiting for observation start')
        self.wait_until_time_or_aborted(self._start_date, self._wait_condition)
        if self.aborted or Time.now() > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Make sure the target is above the horizon
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

        while not self.aborted:
            now = Time.now()
            if now > self._end_date:
                break

            pos = (target - observer).at(timescale.from_astropy(now))
            alt, *_ = pos.altaz()
            _, dec, _ = pos.radec()
            if alt.to(u.deg) > MIN_ALTITUDE * u.deg and dec.to(u.deg) > -45 * u.deg:
                break

            print(f'Target alt is {alt}; dec is {dec}')
            self.set_task('Waiting for target to rise')
            with self._wait_condition:
                self._wait_condition.wait(LOOP_INTERVAL)

        require_onsky = self.config.get('onsky', True)
        if require_onsky and not self.dome_is_open:
            self.set_task('Waiting for dome')
            while not self.dome_is_open and Time.now() <= self._end_date and not self.aborted:
                with self._wait_condition:
                    self._wait_condition.wait(LOOP_INTERVAL)

        if self.aborted or Time.now() > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Note: from this point we'll keep observing, even if the dome closes mid-pass.
        # This keeps the action simple, and we already expect the person reducing the data
        # to have to manually discard frames blocked by the dome walls, W1m dome, etc.
        self.set_task('Acquiring target')
        if not mount_track_tle(self.log_name, self.config['tle']):
            print('failed to track target')
            if self.aborted:
                self.status = TelescopeActionStatus.Complete
            else:
                self.status = TelescopeActionStatus.Error
            return

        # Start science observations
        for camera_id in cameras:
            if camera_id in self.config:
                cam_take_images(self.log_name, camera_id, 0, self.config[camera_id])

        self.set_task(f'Ends {self._end_date.strftime("%H:%M:%S")}')

        # Wait until the target sets or the requested end time
        while True:
            if self.aborted or Time.now() > self._end_date:
                break

            status = mount_status(self.log_name)
            if status.get('alt', 0) < MIN_ALTITUDE:
                break

            with self._wait_condition:
                self._wait_condition.wait(LOOP_INTERVAL)

        mount_stop(self.log_name)
        for camera_id in cameras:
            if camera_id in self.config:
                cam_stop(self.log_name, camera_id)

        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        # Note: mount and cameras will be stopped by the run thread
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
            'required': ['tle', 'start', 'end', 'pipeline'],
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
                'onsky': {'type': 'boolean'}
            }
        }

        schema['properties']['pipeline']['required'].remove('object')

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema(camera_id)

        return validation.validation_errors(config_json, schema)
