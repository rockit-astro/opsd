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

"""Telescope action to find the optimium focus using the v-curve technique"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import datetime
import threading
import numpy as np

from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import log, validation
from warwick.observatory.pipeline import configure_standard_validation_schema as pipeline_schema
from warwick.observatory.camera.qhy import configure_validation_schema as qhy_camera_schema
from .mount_helpers import mount_slew_radec, mount_stop
from .focus_helpers import focus_set, focus_get
from .camera_helpers import cameras, cam_configure, cam_take_images
from .pipeline_helpers import configure_pipeline

SLEW_TIMEOUT = 120
FOCUS_TIMEOUT = 300

CONFIG = {
    # The slope (in hfd / step) on the inside edge of the v-curve
    'inside_focus_slope': -0.0030321,

    # The HFD value where the two v-curve edges cross
    # This is a more convenient way of representing the position intercept difference
    'crossing_hfd': 3.2,

    # Threshold HFD that is used to filter junk
    # Real stars should never be smaller than this
    'minimum_hfd': 3.2,

    # Number of objects that are required to consider MEDHFD valid
    'minimum_object_count': 50,

    # Aim to reach this HFD on the inside edge of the v-curve
    # before offsetting to the final focus
    'target_hfd': 6,

    # Number of measurements to take when moving in to find the target HFD
    'coarse_measure_repeats': 3,

    # Number of measurements to take when sampling the target and final HFDs
    'fine_measure_repeats': 7,

    # Number of focuser steps to move when searching for the target HFD
    'focus_step_size': 1000,

    # Number of seconds to add to the exposure time to account for readout + object detection
    # Consider the frame lost if this is exceeded
    'max_processing_time': 20
}

# Note: pipeline and camera schemas are inserted in the validate_config method
CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['ra', 'dec'],
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
        }
    }
}


class AutoFocusState:
    """Possible states of the AutoFlat routine"""
    MeasureInitial, FindPositionOnVCurve, FindTargetHFD, MeasureTargetHFD, \
        MeasureFinalHFD, Aborting, Complete, Failed, Skipped = range(9)

    Codes = ['I', 'V', 'T', 'M', 'N', 'A', 'C', 'F', 'X']


class CameraWrapper:
    """Holds camera-specific focus state"""
    def __init__(self, camera_id, config, camera_config, log_name):
        self.camera_id = camera_id
        if camera_config:
            self.state = AutoFocusState.MeasureInitial
        else:
            self.state = AutoFocusState.Skipped
        self._log_name = log_name
        self._config = config
        self._camera_config = camera_config
        self._start_time = None
        self._expected_complete = None
        self._initial_focus = None
        self._current_focus = None
        self._measurements = []
        self._failed_measurements = 0
        self._best_hfd = None

    def start(self):
        """Starts the autofocus sequence for this camera"""
        if self.state == AutoFocusState.Skipped:
            return

        self._start_time = datetime.datetime.utcnow()

        # Record the initial focus so we can return on error
        self._initial_focus = self._current_focus = focus_get(self._log_name, self.camera_id)
        if self._initial_focus is None:
            self.state = AutoFocusState.Failed
            return

        # Set the camera config once at the start to avoid duplicate changes
        cam_config = {}
        cam_config.update(self._camera_config)

        if not cam_configure(self._log_name, self.camera_id, cam_config):
            self.state = AutoFocusState.Failed
            return

        # Take the first image to start the process.
        # The main state machine runs inside received_frame.
        self._take_image()

    def _set_failed(self):
        """Restores the original focus position and marks state as failed"""
        if not focus_set(self._log_name, self.camera_id, self._initial_focus, FOCUS_TIMEOUT):
            log.error(self._log_name, 'AutoFocus: camera ' + self.camera_id + ' failed to restore initial focus')
        self.state = AutoFocusState.Failed

    def _take_image(self):
        """Tells the camera to take an exposure."""
        # The current QHY firmware adds an extra exposure time's delay before returning the first frame.
        # Using single frame mode adds even more delay (due to processing time not being overlapped with
        # the next exposure), so add an extra frame's latency here
        self._expected_complete = datetime.datetime.utcnow() \
            + datetime.timedelta(seconds=2*self._camera_config['exposure'] + self._config['max_processing_time'])

        if not cam_take_images(self._log_name, self.camera_id, quiet=True):
            self._set_failed()

    def check_timeout(self):
        """Sets error state if an expected frame is more than 30 seconds late"""
        if self.state > AutoFocusState.Aborting:
            return

        if self._expected_complete and datetime.datetime.utcnow() > self._expected_complete:
            log.error(self._log_name, 'AutoFocus: camera ' + self.camera_id + ' exposure timed out')
            self._set_failed()

    def received_frame(self, headers):
        """Callback to process an acquired frame. headers is a dictionary of header keys"""
        if self.state >= AutoFocusState.Complete:
            return

        if self.state == AutoFocusState.Aborting:
            self._set_failed()
            return

        if 'MEDHFD' not in headers or 'HFDCNT' not in headers:
            log.warning(self._log_name, 'AutoFocus: camera ' + self.camera_id +
                        ' discarding frame without HFD headers')
            self._failed_measurements += 1
            if self._failed_measurements == 5:
                log.error(self._log_name, 'AutoFocus: camera ' + self.camera_id +
                          ' aborting because 5 HFD samples failed')
                self._set_failed()
                return
        else:
            hfd = headers['MEDHFD']
            count = headers['HFDCNT']
            if count > self._config['minimum_object_count'] and hfd > self._config['minimum_hfd']:
                self._measurements.append(hfd)
            else:
                log.warning(self._log_name, 'AutoFocus: camera ' + self.camera_id +
                            ' discarding frame with {} samples ({} HFD)'.format(count, hfd))
                self._failed_measurements += 1
                if self._failed_measurements == 5:
                    log.error(self._log_name, 'AutoFocus: camera ' + self.camera_id +
                              ' aborting because 5 HFD samples failed')
                    self._set_failed()
                    return

        requested = self._config['coarse_measure_repeats']
        if self.state in [AutoFocusState.MeasureTargetHFD, AutoFocusState.MeasureFinalHFD]:
            requested = self._config['fine_measure_repeats']

        if len(self._measurements) == requested:
            print(self.camera_id, ' hfd values:', self._measurements)
            current_hfd = float(np.min(self._measurements))
            log.info(self._log_name, 'AutoFocus: camera ' + self.camera_id +
                     ' HFD at {} steps is {:.1f}" ({} samples)'.format(self._current_focus, hfd, requested))

            self._measurements.clear()
            self._failed_measurements = 0

            if self.state == AutoFocusState.MeasureInitial:
                self.state = AutoFocusState.FindPositionOnVCurve

            if self.state == AutoFocusState.FindPositionOnVCurve:
                # Step inwards until we are well defocused on the inside edge of the v curve
                if self._best_hfd is not None and current_hfd > 2 * self._best_hfd and \
                        current_hfd > self._config['target_hfd']:
                    log.info(self._log_name, 'AutoFocus: camera ' + self.camera_id + ' found position on v-curve')
                    self.state = AutoFocusState.FindTargetHFD
                else:
                    self._current_focus -= self._config['focus_step_size']
                    if not focus_set(self._log_name, self.camera_id, self._current_focus, FOCUS_TIMEOUT):
                        self._set_failed()
                        return

            # Note: not an elif to allow the FindPositionOnVCurve case above to enter this branch too
            if self.state == AutoFocusState.FindTargetHFD:
                # We may have stepped to far inwards in the previous step
                # Step outwards if needed until the current HFD is closer to the target
                if current_hfd > 2 * self._config['target_hfd']:
                    log.info(self._log_name, 'AutoFocus: camera ' + self.camera_id +
                             ' stepping towards HFD {}'.format(self._config['target_hfd']))

                    self._current_focus -= int(current_hfd / (2 * self._config['inside_focus_slope']))
                else:
                    # Do a final move to (approximately) the target HFD
                    self._current_focus += int((self._config['target_hfd'] - current_hfd) /
                                               self._config['inside_focus_slope'])
                    self.state = AutoFocusState.MeasureTargetHFD

                if not focus_set(self._log_name, self.camera_id, self._current_focus, FOCUS_TIMEOUT):
                    self._set_failed()
                    return

            elif self.state == AutoFocusState.MeasureTargetHFD:
                # Jump to target focus using calibrated parameters
                self._current_focus += int((self._config['crossing_hfd'] - current_hfd) /
                                           self._config['inside_focus_slope'])
                self.state = AutoFocusState.MeasureFinalHFD

                if not focus_set(self._log_name, self.camera_id, self._current_focus, FOCUS_TIMEOUT):
                    self._set_failed()
                    return
            elif self.state == AutoFocusState.MeasureFinalHFD:
                runtime = (datetime.datetime.utcnow() - self._start_time).total_seconds()
                log.info(self._log_name, 'AutoFocus: camera ' + self.camera_id +
                         ' achieved HFD of {:.1f}" in {:.0f} seconds'.format(current_hfd, runtime))

                self.state = AutoFocusState.Complete

            if self._best_hfd is None:
                self._best_hfd = current_hfd
            else:
                self._best_hfd = np.fmin(self._best_hfd, current_hfd)

        self._take_image()

    def abort(self):
        """Aborts any active exposures and sets the state to complete"""
        # Assume that focus images are always short and we can just wait for it to finish.
        # The recieved handler will handle restoring the original focus position
        if self.state < AutoFocusState.Complete:
            self.state = AutoFocusState.Aborting


class AutoFocus(TelescopeAction):
    """Telescope action to find the optimium focus using the v-curve technique"""
    def __init__(self, log_name, config):
        super().__init__('Auto Focus', log_name, config)
        self._wait_condition = threading.Condition()
        self._cameras = {}
        for camera_id in cameras:
            self._cameras[camera_id] = CameraWrapper(camera_id, CONFIG, self.config.get(camera_id, None), self.log_name)

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {}
        schema.update(CONFIG_SCHEMA)

        for camera_id in cameras:
            schema['properties'][camera_id] = qhy_camera_schema(camera_id)
        schema['properties']['pipeline'] = pipeline_schema()

        return validation.validation_errors(config_json, schema)

    def __set_failed_status(self):
        """Sets self.status to Complete if aborted otherwise Error"""
        if self.aborted:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def run_thread(self):
        """Thread that runs the hardware actions"""

        # TODO: Add options for start time and expires time

        self.set_task('Slewing to field')

        if not mount_slew_radec(self.log_name, self.config['ra'], self.config['dec'], True, SLEW_TIMEOUT):
            self.__set_failed_status()
            return

        self.set_task('Preparing cameras')

        pipeline_config = {}
        pipeline_config.update(self.config.get('pipeline', {}))
        pipeline_config.update({
            'hfd': True,
            'type': 'JUNK',
            'object': 'AutoFocus',
        })

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.__set_failed_status()
            return

        # This starts the autofocus logic, which is run
        # in the received_frame callbacks
        for camera in self._cameras.values():
            camera.start()

        # Wait until complete
        while True:
            with self._wait_condition:
                self._wait_condition.wait(5)

            codes = ''
            for camera in self._cameras.values():
                camera.check_timeout()
                codes += AutoFocusState.Codes[camera.state]

            self.set_task('Focusing (' + ''.join(codes) + ')')
            if self.aborted:
                break

            if not self.dome_is_open:
                for camera in self._cameras.values():
                    camera.abort()

                print('AutoFocus: Dome has closed')
                log.error(self.log_name, 'AutoFocus: Dome has closed')
                break

            # We are done once all cameras are either complete or have errored
            if all([camera.state >= AutoFocusState.Complete for camera in self._cameras.values()]):
                break

        success = self.dome_is_open and all([camera.state == AutoFocusState.Complete
                                             for camera in self._cameras.values()])

        if self.aborted or success:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        mount_stop(self.log_name)
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
            print('AutoFocus: Ignoring unknown frame')
