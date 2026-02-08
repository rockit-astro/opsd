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

"""Telescope action to slew to a coordinate and acquire a single image"""


import threading
from astropy.time import Time
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cameras, cam_take_images
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_science_schema
from .mount_helpers import mount_slew_radec


class Progress:
    Waiting, Slewing, Acquiring = range(2)


class AcquireImage(TelescopeAction):
    """
    Telescope action to slew to a coordinate and acquire a single image

    Example block:
    {
        "type": "AcquireImage",
        "start": "2022-09-18T22:20:00",
        "end": "2022-09-18T22:30:00",
        "ra": 0,
        "dec": -4.5,
        "cam<1..4>": { # Optional: cameras that aren't listed won't be used
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, gain, offset, stream (advanced options)
        },
        "pipeline": {
           "prefix": "survey",
           "object": "HA 0",
           "archive": ["CAM1"] # Optional: defaults to the cameras defined in the action
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, **args):
        super().__init__('Observe Images', **args)
        self._wait_condition = threading.Condition()

        self._start_date = Time(self.config['start'])
        self._end_date = Time(self.config['end'])
        self._progress = Progress.Waiting

        self._camera_ids = [c for c in cameras if c in self.config]

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []

        if self._progress <= Progress.Waiting and self._start_date:
            tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')

        target_name = self.config["pipeline"]["object"]
        if self._progress <= Progress.Slewing:
            tasks.append(f'Slew to {target_name}')

        if self._progress <= Progress.Acquiring:
            tasks.append('Acquire image')

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Configure pipeline immediately so the dashboard can show target name etc
        pipeline_config = self.config['pipeline'].copy()
        pipeline_config['type'] = 'SCIENCE'
        if 'archive' not in pipeline_config:
            pipeline_config['archive'] = [camera_id.upper() for camera_id in self._camera_ids]

        if not configure_pipeline(self.log_name, self.config['pipeline'], quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        self.wait_until_time_or_aborted(self._start_date, self._wait_condition)
        if Time.now() > self._end_date or self.aborted:
            self.status = TelescopeActionStatus.Complete
            return

        self._progress = Progress.Slewing
        if not mount_slew_radec(self.log_name, self.config['ra'], self.config['dec'], True):
            self.status = TelescopeActionStatus.Error
            return

        self._progress = Progress.Acquiring
        for camera_id in self._camera_ids:
            camera_config = self.config[camera_id].copy()
            camera_config['stream'] = False
            cam_take_images(self.log_name, camera_id, config=camera_config)

        self.wait_until_time_or_aborted(self._end_date, self._wait_condition)
        self.status = TelescopeActionStatus.Complete


    @classmethod
    def config_schema(cls):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['start', 'end', 'ra', 'dec', 'pipeline'],
            'properties': {
                'type': {'type': 'string'},
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'end': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'ra': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 360
                },
                'dec': {
                    'type': 'number',
                    'minimum': -30,
                    'maximum': 85
                },
                'pipeline': pipeline_science_schema()
            }
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema()

        return schema
