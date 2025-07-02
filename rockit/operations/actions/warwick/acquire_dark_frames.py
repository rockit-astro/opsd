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

"""Telescope action to acquire images with the blocking filter"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import threading
from astropy.time import Time
import astropy.units as u
from rockit.common import validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .action_helpers import CameraWrapper, CameraWrapperStatus
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_junk_schema

# Amount of time to wait between camera status checks while observing
CAM_CHECK_STATUS_DELAY = 10 * u.s

class Progress:
    Waiting, Acquiring = range(2)

class AcquireDarkFrames(TelescopeAction):
    """Telescope action to acquire images with the blocking filter"""
    def __init__(self, **args):
        super().__init__('Acquire Dark Frames', **args)
        self._wait_condition = threading.Condition()
        self._progress = Progress.Waiting

        if 'start' in self.config:
            self._start_date = Time(self.config['start'])
        else:
            self._start_date = None

        if 'expires' in self.config:
            self._expires_date = Time(self.config['expires'])
        else:
            self._expires_date = None

        self._camera = CameraWrapper(self)

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []

        if self._progress <= Progress.Waiting:
            if self._start_date:
                tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')

        if self._progress <= Progress.Acquiring:
            tasks.append(f'Acquire {self.config["count"]} images:')
            subtasks = [
                'Filter: BLOCK',
                f'Exposure time: {self.config["camera"]["exposure"]}s'
            ]

            if self._progress == Progress.Acquiring:
                subtasks.append(f'Complete: {self._camera.completed_frames} / {self._camera.target_frames}')

            tasks.append(subtasks)

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        pipeline_config = self.config['pipeline'].copy()
        pipeline_config['type'] = 'DARK' if self.config['camera']['exposure'] > 0 else 'BIAS'
        pipeline_config['archive'] = ['QHY600M']
        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        if self._start_date is not None and Time.now() < self._start_date:
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        if self.aborted:
            self.status = TelescopeActionStatus.Complete
            return

        print('AcquireDarkFrames: starting acquisition')
        camera_config = self.config['camera'].copy()
        camera_config['filter'] = 'BLOCK'
        self._camera.start(camera_config, self.config['count'])

        # Monitor observation status
        self._progress = Progress.Acquiring
        while True:
            if self.aborted:
                break

            self._camera.update()
            if self._camera.status == CameraWrapperStatus.Error:
                self.status = TelescopeActionStatus.Error
                break

            if self._camera.status == CameraWrapperStatus.Stopped:
                self.status = TelescopeActionStatus.Complete
                break

            self.wait_until_time_or_aborted(Time.now() + CAM_CHECK_STATUS_DELAY, self._wait_condition)

        print('AcquireDarkFrames: acquisitions complete')

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        print('AcquireDarkFrames: Got frame')
        self._camera.received_frame(headers)

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['pipeline', 'camera', 'count'],
            'properties': {
                'type': {'type': 'string'},
                'start': {
                    'type': 'string',
                    'format': 'date-time'
                },
                'expires': {
                    'type': 'string',
                    'format': 'date-time'
                },
                'pipeline': pipeline_junk_schema(),
                'count': {
                    'type': 'integer',
                    'minimum': 1
                },
                'camera': camera_science_schema()
            }
        }

        schema['properties']['camera']['properties'].pop('filter')

        return validation.validation_errors(config_json, schema)
