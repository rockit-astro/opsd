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

"""Telescope action to observe a target defined by a Two Line Element orbit"""

# pylint: disable=too-many-branches

import threading
from astropy.time import Time
import astropy.units as u
from skyfield.sgp4lib import EarthSatellite
from skyfield.api import load
from rockit.common import validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .mount_helpers import mount_track_tle, mount_stop, mount_status
from .camera_helpers import cameras, cam_take_images, cam_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import pipeline_science_schema, camera_science_schema

LOOP_INTERVAL = 5
MIN_ALTITUDE = 10


class Progress:
    Waiting, WaitingForTarget, Acquiring, Tracking = range(4)


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
        "cmos": { # Optional: cameras that aren't listed won't be focused
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, window, gain, offset, stream (advanced options)
        },
        "swir": { # Optional: cameras that aren't listed won't be focused
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
    def __init__(self, **args):
        super().__init__('Observe TLE', **args)
        self._wait_condition = threading.Condition()

        self._start_date = Time(self.config['start'])
        self._end_date = Time(self.config['end'])
        self._progress = Progress.Waiting

        self._camera_ids = [c for c in cameras if c in self.config]

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []

        if self._progress <= Progress.Waiting:
            if self._start_date:
                tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')

        if self._progress == Progress.WaitingForTarget:
            if not self.dome_is_open:
                label = 'Wait for dome'
                if self._end_date:
                    label += f' (expires {self._end_date.strftime("%H:%M:%S")})'
            else:
                label = 'Wait for target to rise'
                if self._end_date:
                    label += f' (expires {self._end_date.strftime("%H:%M:%S")})'
            tasks.append(label)

        target_name = self.config["pipeline"].get("object", None)
        if target_name is None:
            target_name = self.config['tle'][0]
            if target_name.startswith('0 '):
                target_name = target_name[2:]

        if self._progress == Progress.Acquiring:
            tasks.append(f'Acquire target {target_name}')
            tasks.append(f'Observe until {self._end_date.strftime("%H:%M:%S")}')

        elif self._progress <= Progress.Tracking:
            tasks.append(f'Observe target {target_name} until {self._end_date.strftime("%H:%M:%S")}')

        return tasks

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

        if not configure_pipeline(self.log_name, pipeline_science_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        self.wait_until_time_or_aborted(self._start_date, self._wait_condition)
        if self.aborted or Time.now() > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Make sure the target is above the horizon
        target = EarthSatellite(
            self.config['tle'][1],
            self.config['tle'][2],
            name=self.config['tle'][0])

        self._progress = Progress.WaitingForTarget
        timescale = load.timescale()

        while not self.aborted:
            now = Time.now()
            if now > self._end_date:
                break

            pos = (target - self.site_location).at(timescale.from_astropy(now))
            alt, *_ = pos.altaz()
            _, dec, _ = pos.radec()
            if alt.to(u.deg) > MIN_ALTITUDE * u.deg and dec.to(u.deg) > -45 * u.deg:
                break

            print(f'Target alt is {alt}; dec is {dec}')
            with self._wait_condition:
                self._wait_condition.wait(LOOP_INTERVAL)

        require_onsky = self.config.get('onsky', True)
        if require_onsky and not self.dome_is_open:
            while not self.dome_is_open and Time.now() <= self._end_date and not self.aborted:
                with self._wait_condition:
                    self._wait_condition.wait(LOOP_INTERVAL)

        if self.aborted or Time.now() > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Note: from this point we'll keep observing, even if the dome closes mid-pass.
        # This keeps the action simple, and we already expect the person reducing the data
        # to have to manually discard frames blocked by the dome walls, W1m dome, etc.
        self._progress = Progress.Acquiring
        if not mount_track_tle(self.log_name, self.config['tle']):
            print('failed to track target')
            if self.aborted:
                self.status = TelescopeActionStatus.Complete
            else:
                # The telescope occasionally fails to lock onto the target TLE (due to e.g. wind)
                # and times out. If we're able to stop the telescope then it is likely to be this
                # rather than something more serious.
                # Skip this action instead of erroring the entire queue!
                if mount_stop(self.log_name):
                    self.status = TelescopeActionStatus.Complete
                else:
                    self.status = TelescopeActionStatus.Error
            return

        # Start science observations
        for camera_id in cameras:
            if camera_id in self.config:
                cam_take_images(self.log_name, camera_id, 0, self.config[camera_id])

        self._progress = Progress.Tracking

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

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        # The leading line number is omitted to keep the string within the 68 character fits limit
        return [
            {'keyword': 'MNTTLE1', 'value': self.config['tle'][1][2:]},
            {'keyword': 'MNTTLE2', 'value': self.config['tle'][2][2:]},
        ]

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
