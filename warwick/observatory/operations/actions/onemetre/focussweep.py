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

"""Telescope action to do a focus sweep using the blue camera on a defined field"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import datetime
import threading
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import validation
from warwick.observatory.pipeline import configure_standard_validation_schema as pipeline_schema
from warwick.observatory.camera.andor import configure_validation_schema as camera_schema
from .telescope_helpers import tel_slew_radec, tel_stop, tel_set_focus
from .camera_helpers import cam_take_images, cam_stop
from .pipeline_helpers import configure_pipeline

SLEW_TIMEOUT = 120
FOCUS_TIMEOUT = 300

# Note: pipeline and camera schemas are inserted in the validate_config method
CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['ra', 'dec', 'min', 'max', 'step'],
    'properties': {
        'type': {'type': 'string'},
        'ra': {
            'type': 'number',
            'minimum': 0,
            'maximum': 360
        },
        'dec': {
            'type': 'number',
            'minimum': -90,
            'maximum': 90
        },
        'min': {
            'type': 'integer',
            'minimum': 0,
            'maximum': 5000
        },
        'max': {
            'type': 'integer',
            'minimum': 0,
            'maximum': 5000
        },
        'step': {
            'type': 'integer',
            'minimum': 0
        }
    }
}


class FocusSweep(TelescopeAction):
    """Telescope action to do a focus sweep on a defined field"""
    def __init__(self, log_name, config):
        super().__init__('Focus Sweep', log_name, config)
        self._wait_condition = threading.Condition()
        self._focus_measurements = {}
        self._camera_id = 'blue'

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {}
        schema.update(CONFIG_SCHEMA)

        # TODO: Support action config for blue or red (telescope or instrument) focus
        schema['properties']['blue'] = camera_schema('blue')
        schema['properties']['pipeline'] = pipeline_schema()

        return validation.validation_errors(config_json, schema)

    def run_thread(self):
        """Thread that runs the hardware actions"""
        self.set_task('Slewing to field')

        if not tel_slew_radec(self.log_name, self.config['ra'], self.config['dec'], True, SLEW_TIMEOUT):
            if not self.aborted:
                self.status = TelescopeActionStatus.Error
            return

        if self.aborted:
            self.status = TelescopeActionStatus.Complete
            return

        self.set_task('Preparing camera')

        pipeline_config = {}
        pipeline_config.update(self.config['pipeline'])
        pipeline_config.update({
            'hfd': True,
            'type': 'SCIENCE',
            'object': 'Focus run',
        })

        if not configure_pipeline(self.log_name, pipeline_config):
            self.status = TelescopeActionStatus.Error
            return

        # Move focuser to the start of the focus range
        current_focus = self.config['min']

        if not tel_set_focus(self.log_name, current_focus, FOCUS_TIMEOUT):
            self.status = TelescopeActionStatus.Error
            return

        # Configure the camera then take the first exposure to start the process
        if not cam_take_images(self.log_name, self._camera_id, 1, self.config[self._camera_id], quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        expected_next_exposure = datetime.datetime.utcnow() + \
            datetime.timedelta(seconds=self.config[self._camera_id]['exposure'] + 10)

        count = int((self.config['max'] - self.config['min']) / self.config['step'])
        while True:
            self.set_task('Measuring position {} / {}'.format(len(self._focus_measurements) + 1, count))

            # The wait period rate limits the camera status check
            # The frame received callback will wake this up immedately
            with self._wait_condition:
                self._wait_condition.wait(10)

            if self.aborted:
                break

            # The last measurement has finished - move on to the next
            if current_focus in self._focus_measurements:
                current_focus += self.config['step']
                if current_focus > self.config['max']:
                    break

                if not tel_set_focus(self.log_name, current_focus, FOCUS_TIMEOUT):
                    self.status = TelescopeActionStatus.Error
                    return

                if not cam_take_images(self.log_name, self._camera_id, 1, self.config[self._camera_id]):
                    self.status = TelescopeActionStatus.Error
                    return

                expected_next_exposure = datetime.datetime.utcnow() \
                    + datetime.timedelta(seconds=self.config[self._camera_id]['exposure'] + 10)

            elif datetime.datetime.utcnow() > expected_next_exposure:
                print('Exposure timed out - retrying')
                if not cam_take_images(self.log_name, self._camera_id, 1, self.config[self._camera_id]):
                    self.status = TelescopeActionStatus.Error
                    return

                expected_next_exposure = datetime.datetime.utcnow() \
                    + datetime.timedelta(seconds=self.config[self._camera_id]['exposure'] + 10)

        if self.aborted or current_focus > self.config['max']:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        tel_stop(self.log_name)
        cam_stop(self.log_name, self._camera_id)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        with self._wait_condition:
            if 'MEDHFD' in headers and 'HFDCNT' in headers and 'TELFOCUS' in headers:
                print('got hfd', headers['MEDHFD'], 'from', headers['HFDCNT'], 'sources')
                self._focus_measurements[headers['TELFOCUS']] = (headers['MEDHFD'], headers['HFDCNT'])
            else:
                print('Headers are missing MEDHFD, HFDCNT, or TELFOCUS')
                print(headers)

            self._wait_condition.notify_all()
