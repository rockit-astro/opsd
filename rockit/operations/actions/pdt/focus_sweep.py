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

"""Telescope action to find focus using the v-curve technique"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import queue
import threading
from astropy.time import Time
from rockit.common import log, validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cameras, cam_configure, cam_take_images
from .focus_helpers import focus_set
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_junk_schema

MAX_PROCESSING_TIME = 30

class Progress:
    Waiting, Focusing = range(2)


class FocusSweep(TelescopeAction):
    """
    Telescope action to acquire images over a set of focus values

    Example block:
    {
        "type": "FocusSweep",
        "start": "2022-09-18T22:20:00", # Optional: defaults to immediately
        "expires": "2022-09-18T22:30:00", # Optional: defaults to never
        "cam1": { # Optional: cameras that aren't listed won't be focused
            "focus": [0.1, 0.2, 0.3, 0.4, 0.5],
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, window, gain, offset, stream (advanced options)
        },
        "cam2": { # Optional: cameras that aren't listed won't be focused
            "focus": [0.1, 0.2, 0.3, 0.4, 0.5],
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, window, gain, offset, stream (advanced options)
        },
        "pipeline": {
           "prefix": "focussweep",
           "archive": ["CAM1"] # Optional: defaults to "camera"
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, **args):
        super().__init__('Focus Sweep', **args)
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

        self._cameras = {}
        for camera_id in cameras:
            self._cameras[camera_id] = CameraWrapper(camera_id, self.config.get(camera_id, None), self.log_name)

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []

        if self._progress <= Progress.Waiting and self._start_date is not None and Time.now() < self._start_date:
            tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')
        elif not self.dome_is_open:
            label = 'Wait for dome'
            if self._expires_date:
                label += f' (expires {self._expires_date.strftime("%H:%M:%S")})'
            tasks.append(label)

        if self._progress < Progress.Focusing:
            camera_ids = [c.camera_id for c in self._cameras.values() if c.state != FocusSweepState.Complete]
            tasks.append(f'Run Focus Sweep ({", ".join(camera_ids)})')
        elif self._progress == Progress.Focusing:
            tasks.append('Run Focus Sweep:')
            camera_state = []
            for camera_id, camera in self._cameras.items():
                camera_state.append(f'{camera_id}: {FocusSweepState.Labels[camera.state]}')
            tasks.append(camera_state)

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        if self._start_date is not None and Time.now() < self._start_date:
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        while not self.aborted and not self.dome_is_open:
            if self._expires_date is not None and Time.now() > self._expires_date:
                break

            with self._wait_condition:
                self._wait_condition.wait(10)

        if self.aborted or self._expires_date is not None and Time.now() > self._expires_date:
            self.status = TelescopeActionStatus.Complete
            return

        self._progress = Progress.Focusing

        pipeline_config = self.config['pipeline'].copy()
        if 'archive' not in pipeline_config:
            pipeline_config['archive'] = [c.upper() for c in self._cameras]

        pipeline_config.update({
            'type': 'JUNK',
            'hfd': True
        })

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        # This starts the focus sweep logic, which is run on camera-specific threads
        for camera in self._cameras.values():
            camera.start()

        # Wait until complete
        while True:
            with self._wait_condition:
                self._wait_condition.wait(5)

            if self.aborted:
                break

            if not self.dome_is_open:
                for camera in self._cameras.values():
                    camera.abort()

                log.error(self.log_name, 'Focus Sweep: Dome has closed')
                break

            # We are done once all cameras are either complete or have errored
            if all(camera.state >= FocusSweepState.Complete for camera in self._cameras.values()):
                break

        if any(camera.state == FocusSweepState.Error for camera in self._cameras.values()):
            self.status = TelescopeActionStatus.Error
        else:
            self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        for camera in self._cameras.values():
            camera.abort()

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        camera_id = headers.get('CAMID', '').lower()
        if camera_id in self._cameras:
            self._cameras[camera_id].received_frame(headers)
        else:
            print('FocusSweep: Ignoring unknown frame')

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': [],
            'properties': {
                'type': {'type': 'string'},
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'expires': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'pipeline': pipeline_junk_schema(),
            },
            'dependencies': {
                'start': ['expires']
            }
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema()
            schema['properties'][camera_id]['properties']['focus'] = {
                'type': 'array',
                'minItems': 1,
                'items': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 50
                }
            }

        return validation.validation_errors(config_json, schema)


class FocusSweepState:
    """Possible states of the FocusSweep routine"""
    Measuring, Aborting, Complete, Failed, Error = range(5)

    Labels = {
        0: 'Measuring',
        1: 'Aborting',
        2: 'Complete',
        3: 'Failed',
        4: 'Error'
    }


class CameraWrapper:
    """Holds camera-specific focus state"""
    def __init__(self, camera_id, config, log_name):
        self.camera_id = camera_id
        if config is not None:
            self.state = FocusSweepState.Measuring
        else:
            self.state = FocusSweepState.Complete

        self._log_name = log_name
        self._config = config
        self._focus_steps = config.pop('focus')
        self._received_queue = queue.Queue()

    def _run(self):
        """Thread running the main state machine"""
        exposure_timeout = (self._config['exposure'] + MAX_PROCESSING_TIME)

        cam_config = self._config.copy()
        cam_config['stream'] = False
        if not cam_configure(self._log_name, self.camera_id, cam_config):
            self.state = FocusSweepState.Error
            return

        class Failed(Exception):
            pass

        class Error(Exception):
            pass

        try:
            failed_measurements = 0
            while True:
                if not self._focus_steps:
                    self.state = FocusSweepState.Complete
                    break

                if not focus_set(self._log_name, self.camera_id, self._focus_steps.pop(0)):
                    raise Error

                while True:
                    if not cam_take_images(self._log_name, self.camera_id, quiet=True):
                        raise Error
                    try:
                        self._received_queue.get(timeout=exposure_timeout)
                        break
                    except queue.Empty:
                        log.error(self._log_name, f'FocusSweep: camera {self.camera_id} exposure timed out')
                        failed_measurements += 1
                        if failed_measurements > 5:
                            log.error(self._log_name, f'FocusSweep: camera {self.camera_id} aborting because 5 HFD samples failed')
                            raise Failed

        except Failed:
            self.state = FocusSweepState.Failed
        except Exception:
            self.state = FocusSweepState.Error

    def start(self):
        """Starts the focus sweep for this camera"""
        if self.state == FocusSweepState.Complete:
            return

        threading.Thread(target=self._run).start()

    def abort(self):
        """Aborts any active exposures and sets the state to complete"""
        # Assume that focus images are always short so we can just wait for the state machine to clean up
        if self.state < FocusSweepState.Complete:
            self.state = FocusSweepState.Aborting

    def received_frame(self, headers):
        """Callback to process an acquired frame. headers is a dictionary of header keys"""
        if self.state >= FocusSweepState.Complete:
            return

        self._received_queue.put((headers.get('MEDHFD', None)))
