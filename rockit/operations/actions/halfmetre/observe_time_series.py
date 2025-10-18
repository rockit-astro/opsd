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

"""Telescope action to observe a sidereally tracked field"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

from collections import deque
import sys
import threading
import traceback

from astropy.time import Time
import astropy.units as u
import numpy as np
from rockit.common import log, validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .action_helpers import CameraWrapper, CameraWrapperStatus, FieldAcquisitionHelper, PIDController, cross_correlate
from .mount_helpers import mount_offset_radec, mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_science_schema

# Amount of time to wait between camera status checks while observing
CAM_CHECK_STATUS_DELAY = 10 * u.s

# Amount of time to wait for cameras to report the stopped status before giving up
CAM_STOP_TIMEOUT = 60 * u.s

# Track a limited history of shifts so we can handle outliers
GUIDE_BUFFER_REJECTION_SIGMA = 10
GUIDE_BUFFER_LENGTH = 20

# Shifts larger than this are automatically rejected without touching the guide buffer
GUIDE_MAX_PIXEL_ERROR = 100

# PID loop coefficients
GUIDE_PID = [0.75, 0.02, 0.0]


class ObservationStatus:
    PositionLost, OnTarget, DomeClosed, Complete, Error = range(5)


class Progress:
    Waiting, Acquiring, Observing = range(3)


class ObserveTimeSeries(TelescopeAction):
    """Telescope action to observe a sidereally tracked field"""
    def __init__(self, **args):
        super().__init__('Observe Time Series', **args)
        self._wait_condition = threading.Condition()

        self._start_date = Time(self.config['start'])
        self._end_date = Time(self.config['end'])
        self._progress = Progress.Waiting

        self._observation_status = ObservationStatus.PositionLost
        self._is_guiding = False
        self._guide_reference_expcount = None
        self._guide_filename = None
        self._guide_profiles = None
        self._guide_accumulated_ra = 0
        self._guide_accumulated_dec = 0
        self._guide_last_updated = None

        self._camera = CameraWrapper(self)
        self._acquisition_helper = FieldAcquisitionHelper(self)
        self._guide_buff_x = deque(maxlen=GUIDE_BUFFER_LENGTH)
        self._guide_buff_y = deque(maxlen=GUIDE_BUFFER_LENGTH)
        self._guide_pid_x = PIDController(*GUIDE_PID)
        self._guide_pid_y = PIDController(*GUIDE_PID)

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []

        if self._progress <= Progress.Waiting:
            if self._start_date:
                tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')
        elif not self.dome_is_open:
            tasks.append('Wait for dome')

        target_name = self.config["pipeline"]["object"]
        if self._progress <= Progress.Acquiring:
            tasks.append(f'Acquire target field for {target_name}')
            if 'blind_offset_dra' in self.config:
                dra = self.config['blind_offset_dra']
                ddec = self.config['blind_offset_ddec']
                tasks.append(f'Using blind offset: {dra:.3f}, {ddec:.3f} deg')
            tasks.append(f'Observe until {self._end_date.strftime("%H:%M:%S")}')

        elif self._progress <= Progress.Observing:
            tasks.append(f'Observe target {target_name} until {self._end_date.strftime("%H:%M:%S")}')

        exposure = self.config['camera']['exposure']
        tasks.append([
            f'Exposure time: {exposure}s',
            'Autoguiding: enabled'
        ])
        return tasks

    def __acquire_field(self):
        self._progress = Progress.Acquiring

        # Point to the requested location
        print('ObserveTimeSeries: slewing to target field')
        blind_offset_dra = self.config.get('blind_offset_dra', 0)
        blind_offset_ddec = self.config.get('blind_offset_ddec', 0)
        acquisition_ra = self.config['ra'] + blind_offset_dra
        acquisition_dec = self.config['dec'] + blind_offset_ddec
        acquisition_exposure = self.config.get('acquisition_exposure', 5)
        if not self._acquisition_helper.acquire_field(acquisition_ra, acquisition_dec, acquisition_exposure):
            return ObservationStatus.Error

        if blind_offset_dra != 0 or blind_offset_ddec != 0:
            print('ObserveTimeSeries: Offsetting to target')
            if not mount_offset_radec(self.log_name, -blind_offset_dra, -blind_offset_ddec):
                return ObservationStatus.Error

        return ObservationStatus.OnTarget

    def __wait_for_dome(self):
        self._progress = Progress.Waiting
        while True:
            with self._wait_condition:
                if Time.now() > self._end_date or self.aborted:
                    return ObservationStatus.Complete

                if self.dome_is_open:
                    return ObservationStatus.PositionLost

                self._wait_condition.wait(10)

    def __observe_field(self):
        # Start science observations
        pipeline_config = self.config['pipeline'].copy()
        pipeline_config['guide'] = 'HALFMETRE'
        pipeline_config['type'] = 'SCIENCE'
        pipeline_config['archive'] = ['HALFMETRE']

        if not configure_pipeline(self.log_name, pipeline_config):
            return ObservationStatus.Error

        print('ObserveTimeSeries: starting science observations')
        self._camera.start(self.config['camera'])
        self._is_guiding = True

        # Monitor observation status
        self._progress = Progress.Observing
        return_status = ObservationStatus.Complete
        while True:
            if self.aborted or Time.now() > self._end_date:
                break

            if not self.dome_is_open:
                log.error(self.log_name, 'Aborting because dome is not open')
                return_status = ObservationStatus.DomeClosed
                break

            if not self._is_guiding:
                log.warning(self.log_name, 'Lost autoguiding lock')
                return_status = ObservationStatus.PositionLost
                break

            self._camera.update()
            if self._camera.status == CameraWrapperStatus.Error:
                return_status = ObservationStatus.Error
                break

            self.wait_until_time_or_aborted(Time.now() + CAM_CHECK_STATUS_DELAY, self._wait_condition)

        # Wait for all cameras to stop before returning to the main loop
        print('ObserveTimeSeries: stopping science observations')
        self._is_guiding = False
        self._camera.stop()

        start = Time.now()
        while True:
            if self._camera.status in [CameraWrapperStatus.Error, CameraWrapperStatus.Stopped]:
                print('ObserveTimeSeries: camera has stopped')
                break

            if (Time.now() - start) > CAM_STOP_TIMEOUT:
                print('ObserveTimeSeries: timeout waiting for camera to stop')
                break

            self._camera.update()

            with self._wait_condition:
                self._wait_condition.wait(CAM_CHECK_STATUS_DELAY.to_value(u.s))

        return return_status

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Configure pipeline immediately so the dashboard can show target name etc
        if not configure_pipeline(self.log_name, self.config['pipeline'], quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        self.wait_until_time_or_aborted(self._start_date, self._wait_condition)
        if Time.now() > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Outer loop handles transitions between states
        # Each method call blocks, returning only when it is ready to exit or switch to a different state
        while True:
            if self._observation_status == ObservationStatus.Error:
                print('ObserveTimeSeries: status is now Error')
                break

            if self._observation_status == ObservationStatus.Complete:
                print('ObserveTimeSeries: status is now Complete')
                break

            if self._observation_status == ObservationStatus.OnTarget:
                print('ObserveTimeSeries: status is now OnTarget')
                self._observation_status = self.__observe_field()

            if self._observation_status == ObservationStatus.PositionLost:
                print('ObserveTimeSeries: status is now PositionLost')
                self._observation_status = self.__acquire_field()

            if self._observation_status == ObservationStatus.DomeClosed:
                print('ObserveTimeSeries: status is now DomeClosed')
                self._observation_status = self.__wait_for_dome()

        mount_stop(self.log_name)

        if self._observation_status == ObservationStatus.Complete:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        self._acquisition_helper.aborted_or_dome_status_changed()

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)
        self._acquisition_helper.aborted_or_dome_status_changed()

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        print('ObserveTimeSeries: Got frame')
        self._acquisition_helper.received_frame(headers)
        self._camera.received_frame(headers)

        # The FILENAME header key is not available when received_guide_profile is called
        # so we instead save the exposure count number to match against filename here
        if self._guide_reference_expcount is not None and self._guide_filename is None:
            if headers.get('EXPCNT', None) == self._guide_reference_expcount:
                self._guide_filename = headers.get('FILENAME', None)

    def received_guide_profile(self, headers, profile_x, profile_y):
        """Notification called when a guide profile has been calculated by the data pipeline"""
        if not self._is_guiding:
            return None

        if self._guide_profiles is None:
            print('ObserveTimeSeries: set reference guide profiles')
            self._guide_reference_expcount = headers.get('EXPCNT', None)
            self._guide_profiles = profile_x, profile_y
            self._guide_accumulated_ra = 0
            self._guide_accumulated_dec = 0
            return None

        # Status flags:
        #    0x01: Image started exposing before the last guide correction was applied
        #    0x02: Pixel offset is larger than the maximum allowed value
        #    0x04: Pixel offset is more than 10 sigma away from the previous offset history
        #    0x08: Mount offset failed
        # A value of 0 means that the correction was valid and applied
        guide_flags = 0

        if self._guide_filename:
            guide_headers = [{
                "keyword": "AGREFIMG",
                "value": self._guide_filename,
                "comment": "filename of autoguider reference image"
            }]
        else:
            guide_headers = [{
                "keyword": "COMMENT",
                "value": " AGREFIMG not available",
            }]

        try:
            # Measure image offset
            dx = cross_correlate(profile_x, self._guide_profiles[0])
            dy = cross_correlate(profile_y, self._guide_profiles[1])
            print(f'ObserveTimeSeries: measured guide offsets {dx:.2f} {dy:.2f} px')

            guide_headers.append({
                "keyword": "AG_ERRX",
                "value": round(dx, 2),
                "comment": "[px] autoguider measured x-axis offset"
            })

            guide_headers.append({
                "keyword": "AG_ERRY",
                "value": round(dy, 2),
                "comment": "[px] autoguider measured y-axis offset"
            })

            guide_date = headers.get('DATE-OBS', None)

            if self._guide_last_updated and guide_date and Time(guide_date) < self._guide_last_updated:
                print(f'ObserveTimeSeries: {guide_date} < last offset date ({self._guide_last_updated.isot})')
                print('ObserveTimeSeries: Skipping this correction')
                guide_flags += 0x01
                return None

            # Ignore suspiciously big shifts
            if abs(dx) > GUIDE_MAX_PIXEL_ERROR or abs(dy) > GUIDE_MAX_PIXEL_ERROR:
                print(f'ObserveTimeSeries: Offset larger than max allowed pixel shift: x: {dx} y:{dy}')
                print('ObserveTimeSeries: Skipping this correction')
                guide_flags += 0x02
                return None

            # Store the pre-pid values in the buffer
            self._guide_buff_x.append(dx)
            self._guide_buff_y.append(dx)

            # Ignore shifts that are inconsistent with previous shifts,
            # but only after we have collected enough measurements to trust the stats
            if len(self._guide_buff_x) == self._guide_buff_x.maxlen:
                if abs(dx) > GUIDE_BUFFER_REJECTION_SIGMA * np.std(self._guide_buff_x) or \
                        abs(dy) > GUIDE_BUFFER_REJECTION_SIGMA * np.std(self._guide_buff_y):
                    print(f'ObserveTimeSeries: Guide correction(s) too large x:{dx:.2f} y:{dy:.2f}')
                    print('ObserveTimeSeries: Skipping this correction but adding to stats buffer')
                    guide_flags += 0x04
                    return None

            # Generate the corrections from the PID controllers
            corr_dx = -self._guide_pid_x.update(dx)
            corr_dy = -self._guide_pid_y.update(dy)
            print(f'ObserveTimeSeries: post-PID corrections {corr_dx:.2f} {corr_dy:.2f} px')

            guide_headers.append({
                "keyword": "AG_CORRX",
                "value": round(corr_dx, 2),
                "comment": "[px] autoguider x-axis correction"
            })

            guide_headers.append({
                "keyword": "AG_CORRY",
                "value": round(corr_dy, 2),
                "comment": "[px] autoguider y-axis correction"
            })

            pixels_to_degrees = self._acquisition_helper.wcs_derivatives
            corr_dra = pixels_to_degrees[0, 0] * corr_dx + pixels_to_degrees[0, 1] * corr_dy
            corr_ddec = pixels_to_degrees[1, 0] * corr_dx + pixels_to_degrees[1, 1] * corr_dy
            print(f'ObserveTimeSeries: post-PID corrections {corr_dra * 3600:.2f} {corr_ddec * 3600:.2f} arcsec')

            self._guide_accumulated_ra += corr_dra
            self._guide_accumulated_dec += corr_ddec

            guide_headers.append({
                "keyword": "AG_CORRR",
                "value": round(3600 * corr_dra, 2),
                "comment": "[arcsec] autoguider ra correction"
            })

            guide_headers.append({
                "keyword": "AG_CORRD",
                "value": round(3600 * corr_ddec, 2),
                "comment": "[arcsec] autoguider dec correction"
            })

            # TODO: reacquire using WCS (self._is_guiding = False) if we detect things have gone wrong

            # Apply correction
            if not mount_offset_radec(self.log_name, corr_dra, corr_ddec):
                print('ObserveTimeSeries: Mount offset failed')
                guide_flags += 0x08

            self._guide_last_updated = Time.now()
        except Exception:
            traceback.print_exc(file=sys.stdout)
            self._is_guiding = False
        finally:
            if len(guide_headers) == 3:
                for key in ['AG_CORRX', 'AG_CORRY', 'AG_CORRR', 'AG_CORRD']:
                    guide_headers.append({
                        "keyword": "COMMENT",
                        "value": f" {key} not available",
                    })

            guide_headers.append({
                "keyword": "AG_DELTR",
                "value": round(3600 * self._guide_accumulated_ra, 2),
                "comment": "[arcsec] autoguider accumulated ra correction"
            })

            guide_headers.append({
                "keyword": "AG_DELTD",
                "value": round(3600 * self._guide_accumulated_dec, 2),
                "comment": "[arcsec] autoguider accumulated dec correction"
            })

            guide_headers.append({
                "keyword": "AG_FLAGS",
                "value": guide_flags,
                "comment": "autoguider status flags"
            })

            return guide_headers

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['start', 'end', 'ra', 'dec', 'pipeline', 'camera'],
            'properties': {
                'type': {'type': 'string'},
                'start': {
                    'type': 'string',
                    'format': 'date-time'
                },
                'end': {
                    'type': 'string',
                    'format': 'date-time'
                },
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
                'blind_offset_dra': {
                    'type': 'number'
                },
                'blind_offset_ddec': {
                    'type': 'number'
                },
                'acquisition_exposure': {
                    'type': 'number',
                    'minimum': 0
                },
                'pipeline': pipeline_science_schema(),
                'camera': camera_science_schema()
            },
            'dependencies': {
                'blind_offset_dra': ['blind_offset_ddec'],
                'blind_offset_ddec': ['blind_offset_dra']
            }
        }

        return validation.validation_errors(config_json, schema)