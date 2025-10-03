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

"""Telescope action to observe a target defined by a custom UTC/RA/Dec path"""

# pylint: disable=too-many-branches

import sys
import threading
import traceback
from astropy.time import Time
from rockit.common import validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .mount_helpers import mount_track_path, mount_stop
from .camera_helpers import (cameras, cam_configure, cam_reinitialize_synchronised,
                             cam_start_synchronised, cam_stop_synchronised)
from .pipeline_helpers import configure_pipeline
from .schema_helpers import pipeline_science_schema, camera_science_schema

LOOP_INTERVAL = 5

class Progress:
    Waiting, WaitingForDome, Acquiring, Tracking = range(4)


class ObservePathTracking(TelescopeAction):
    """
    Telescope action to observe a target defined by a custom UTC/RA/Dec path

    Example block:
    {
        "type": "ObservePathTracking",
        "start": "2022-09-18T22:20:00",
        "end": "2022-09-18T22:30:00",
        "onsky": true, # Optional: defaults to true
        "path": [
            ["2022-09-18T22:20:00", 15.0, 0], # utc, ra, dec
            ["2022-09-18T22:30:00", 20.0, 2], # utc, ra, dec
            ["2022-09-18T22:40:00", 25.0, 5], # utc, ra, dec
        ],
        "cam<1..4>": { # Optional: cameras that aren't listed won't be used
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, window, gain, offset, stream (advanced options)
        },
        "pipeline": {
           "prefix": "path",
           "object": "Custom Path",
           "archive": ["CAM1"] # Optional: defaults to the cameras defined in the action
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, **args):
        super().__init__('Observe Path', **args)
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

        if self._progress == Progress.WaitingForDome:
            label = 'Wait for dome'
            if self._end_date:
                label += f' (expires {self._end_date.strftime("%H:%M:%S")})'
            tasks.append(label)

        target_name = self.config['pipeline']['object']
        if self._progress == Progress.Acquiring:
            tasks.append('Acquire path')

        if self._progress <= Progress.Tracking:
            tasks.append(f'Observe {target_name} until {self._end_date.strftime("%H:%M:%S")}')

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Configure pipeline immediately so the dashboard can show target name etc
        pipeline_science_config = self.config['pipeline'].copy()
        pipeline_science_config['type'] = 'SCIENCE'
        if 'archive' not in pipeline_science_config:
            pipeline_science_config['archive'] = [camera_id.upper() for camera_id in self._camera_ids]

        if not configure_pipeline(self.log_name, pipeline_science_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        self.wait_until_time_or_aborted(self._start_date, self._wait_condition)
        if self.aborted or Time.now() > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        require_onsky = self.config.get('onsky', True)
        if require_onsky and not self.dome_is_open:
            self._progress = Progress.WaitingForDome
            while not self.dome_is_open and Time.now() <= self._end_date and not self.aborted:
                with self._wait_condition:
                    self._wait_condition.wait(LOOP_INTERVAL)

        if self.aborted or Time.now() > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Note: from this point we'll keep observing, even if the dome closes mid-pass.
        # This keeps the action simple, and we already expect the person reducing the data
        # to have to manually discard frames blocked by the dome walls, W1m dome, etc.
        # TODO: This is true for LEO, but tracked GEO obs really should pause...
        self._progress = Progress.Acquiring
        if not mount_track_path(self.log_name, self.config['path']):
            print('failed to track path')
            if self.aborted:
                self.status = TelescopeActionStatus.Complete
            else:
                self.status = TelescopeActionStatus.Error
            return

        # Start science observations
        success = cam_reinitialize_synchronised(self.log_name, self._camera_ids, attempts=3)
        for camera_id in self._camera_ids:
            success = success and cam_configure(self.log_name, camera_id, self.config[camera_id], quiet=True)

        success = success and cam_start_synchronised(self.log_name, self._camera_ids)
        if not success:
            print('failed to start exposures')
            if self.aborted:
                self.status = TelescopeActionStatus.Complete
            else:
                self.status = TelescopeActionStatus.Error
            return

        self._progress = Progress.Tracking

        # Wait until the requested end time
        while True:
            if self.aborted or Time.now() > self._end_date:
                break

            with self._wait_condition:
                self._wait_condition.wait(LOOP_INTERVAL)

        mount_stop(self.log_name)

        # Wait for all cameras to stop before returning to the main loop
        cam_stop_synchronised(self.log_name, self._camera_ids)

        self.status = TelescopeActionStatus.Complete

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        keys = []
        try:
            # Include all path nodes within the exposure plus one before/after
            start = 0
            end = len(self.config['path'])
            date_start = Time(headers['DATE-OBS'])
            date_end = Time(headers['DATE-END'])
            date_path = Time([x[0] for x in self.config['path']])

            # Bogus path?!?
            if date_end < date_path[0] or date_start > date_path[-1]:
                return keys

            for i in range(len(date_path) - 1):
                if date_path[i] < date_start < date_path[i + 1]:
                    start = i

                if date_path[i] < date_end < date_path[i + 1]:
                    end = i + 2

            for i, j in enumerate(range(start, end)):
                utc, ra, dec = self.config['path'][j]
                keys.extend([
                    {'keyword': f'PATHT{i:03d}', 'value': utc},
                    {'keyword': f'PATHR{i:03d}', 'value': ra},
                    {'keyword': f'PATHD{i:03d}', 'value': dec},
                ])
        except Exception:
            traceback.print_exc(file=sys.stdout)

        return keys

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
            'required': ['path', 'start', 'end', 'pipeline'],
            'properties': {
                'type': {'type': 'string'},
                'path': {
                    'type': 'array',
                    'items': {
                        'type': 'array',
                        'maxItems': 3,
                        'minItems': 3,
                        'items': [
                            {
                                'type': 'string',
                                'format': 'date-time'
                            },
                            {
                                'type': 'number',
                                'minimum': 0,
                                'maximum': 360
                            },
                            {
                                'type': 'number',
                                'minimum': -30,
                                'maximum': 90
                            }
                        ]
                    }
                },
                'start': {
                    'type': 'string',
                    'format': 'date-time'
                },
                'end': {
                    'type': 'string',
                    'format': 'date-time'
                },
                'pipeline': pipeline_science_schema(),
                'onsky': {'type': 'boolean'}
            }
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema()

        return validation.validation_errors(config_json, schema)
