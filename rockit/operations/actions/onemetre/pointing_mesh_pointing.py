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

"""Telescope action to measure a pointing model point at a given alt az"""

# pylint: disable=too-many-branches

import threading
from astropy.time import Time
import astropy.units as u
from rockit.common import validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cam_take_images
from .coordinate_helpers import altaz_to_radec
from .mount_helpers import mount_slew_radec, mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_junk_schema

MAX_PROCESSING_TIME = 30 * u.s
DOME_CHECK_INTERVAL = 10


class Progress:
    NotStarted, Waiting, Slewing, Measuring = range(4)


class PointingMeshPointing(TelescopeAction):
    """
    Telescope action to acquire a sidereally tracked image at a given alt, az for calibrating a pointing mesh

    Example block:
    {
        "type": "PointingMeshPointing",
        "alt": 50,
        "az": 180,
        "camera": {
            "exposure": 1
            # Also supports optional bin, window, temperature, gainindex, readoutindex (advanced options)
        },
        "pipeline": {
           "prefix": "pointing",
           "archive": ["BLUE"] # Optional: defaults to cameras specified above
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, **args):
        super().__init__('Pointing Model', **args)
        self._wait_condition = threading.Condition()
        self._progress = Progress.NotStarted
        self._received_frames = []

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []
        if self._progress == Progress.Waiting and not self.dome_is_open:
            tasks.append('Wait for dome')
        if self._progress <= Progress.Slewing:
            tasks.append(f'Slew to alt {round(self.config["alt"])}\u00B0, az {round(self.config["az"])}\u00B0')
        if self._progress <= Progress.Measuring:
            tasks.append('Acquire image')

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        while not self.aborted and not self.dome_is_open:
            self._progress = Progress.Waiting
            with self._wait_condition:
                self._wait_condition.wait(DOME_CHECK_INTERVAL)

        if self.aborted:
            self.status = TelescopeActionStatus.Complete
            return

        self._progress = Progress.Measuring

        # Take a frame to solve field center
        pipeline_config = self.config['pipeline'].copy()
        pipeline_config['wcs'] = True
        if 'archive' not in pipeline_config:
            pipeline_config['archive'] = ['QHY600M']

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        ra, dec = altaz_to_radec(self.site_location, self.config['alt'], self.config['az'])
        if not mount_slew_radec(self.log_name, ra, dec, tracking=True, open_covers=True):
            self.status = TelescopeActionStatus.Complete
            return

        max_exposure = 0
        print('PointingMeshPointing: taking image')
        cam_take_images(self.log_name, 1, self.config['camera'], quiet=True)
        max_exposure = max(max_exposure, self.config['camera']['exposure'])

        # Wait for new frame
        expected_complete = Time.now() + max_exposure * u.s + MAX_PROCESSING_TIME

        while True:
            with self._wait_condition:
                remaining = (expected_complete - Time.now()).to(u.second).value
                if remaining < 0 or self._received_frames:
                    break

                self._wait_condition.wait(max(remaining, 1))

        mount_stop(self.log_name)

        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        mount_stop(self.log_name)

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
        with self._wait_condition:
            self._received_frames.append(camera_id)
            self._wait_condition.notify_all()

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['alt', 'az', 'pipeline', 'camera'],
            'properties': {
                'type': {'type': 'string'},
                'alt': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 90
                },
                'az': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 360
                },
                'camera': camera_science_schema(),
                'pipeline': pipeline_junk_schema()
            }
        }

        return validation.validation_errors(config_json, schema)
