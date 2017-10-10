#!/usr/bin/env python3
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

"""Class managing automatic telescope control for the operations daemon"""

# pylint: disable=too-few-public-methods
# pylint: disable=too-many-statements
# pylint: disable=too-many-branches
# pylint: disable=too-many-locals
# pylint: disable=too-many-return-statements
# pylint: disable=invalid-name
# pylint: disable=broad-except

import datetime
import threading
import time
from astropy.coordinates import SkyCoord
from astropy import units as u
from astropy.wcs import WCS
from warwick.observatory.common import (
    daemons,
    log,
    TryLock)
from .constants import CommandStatus

class AutoFlatState:
    """Possible states of the AutoFlat routine"""
    Bias, Waiting, Saving, Complete = range(4)
    Names = ['Bias', 'Waiting', 'Saving', 'Complete']

# This should be kept in sync with the dictionary in ops
class CameraStatus:
    """Camera status, from camd"""
    Disabled, Initializing, Idle, Acquiring, Reading, Aborting = range(6)

class TelescopeController(object):
    """Class managing automatic telescope control for the operations daemon"""
    def __init__(self, autoflat_config, autoacquire_config):
        self._frame_watchers = []
        self._telescope_lock = threading.Lock()
        self._telescope_condition = threading.Condition()
        self._telescope_active = False
        self._autoflat_config = autoflat_config
        self._autoacquire_config = autoacquire_config

    def notify_processed_frame(self, headers):
        """Called by the pipeline daemon to notify that a new frame has completed processing
           headers is a dictionary holding the key-value pairs from the fits header"""
        for f in self._frame_watchers:
            f(headers)

    # TODO: Rewrite using a dedicated telescope thread and action classes (see telcontrol branch)
    def autoflat(self):
        """Placeholder logic to run the cameras to acquire flat frames with variable exposures"""
        config = self._autoflat_config
        with TryLock(self._telescope_lock) as success:
            if not success:
                return CommandStatus.Blocked

            bias = {
                'BLUE': 0,
                'RED': 0
            }

            state = {
                'BLUE': AutoFlatState.Bias,
                'RED': AutoFlatState.Bias
            }

            camera_daemons = {
                'BLUE': daemons.onemetre_blue_camera,
                'RED': daemons.onemetre_red_camera
            }

            # Account for the setting or rising sun
            is_evening = datetime.datetime.utcnow().hour > 12
            scale = config['evening_scale'] if is_evening else config['dawn_scale']

            def received_frame(headers):
                """Callback to process an acquired frame.  headers is a dictionary of header keys"""
                arm = None
                try:
                    arm = headers['INSTRARM']
                    last_state = state[arm]

                    if state[arm] == AutoFlatState.Bias:
                        bias[arm] = headers['MEDCNTS']
                        print(arm + ' bias level is {:.0f} ADU'.format(bias[arm]))
                        log.info('opsd', '{} bias is {:.0f} ADU'.format(arm, bias[arm]))

                        # Take the first flat image
                        state[arm] = AutoFlatState.Waiting
                        with camera_daemons[arm].connect() as cam:
                            start_exp = config['min_exposure'] if is_evening \
                                else config['min_save_exposure']
                            cam.set_exposure(start_exp)
                            cam.set_shutter(True)
                            cam.start_sequence(1)

                    elif state[arm] == AutoFlatState.Waiting or state[arm] == AutoFlatState.Saving:
                        exposure = headers['EXPTIME']
                        counts = headers['MEDCNTS'] - bias[headers['INSTRARM']]

                        # If the count rate is too low then we scale the exposure by the max amount
                        if counts > 0:
                            new_exposure = scale * exposure * config['target_counts'] / counts
                        else:
                            new_exposure = exposure * config['max_exposure_delta']

                        # Clamp the exposure to a sensible range
                        clamped_exposure = min(new_exposure, config['max_exposure'],
                                               exposure * config['max_exposure_delta'])
                        clamped_exposure = max(clamped_exposure, config['min_exposure'],
                                               exposure / config['max_exposure_delta'])

                        clamped_desc = ' (clamped from {:.2f}s)'.format(new_exposure) \
                            if new_exposure > clamped_exposure else ''
                        print(arm + ' exposure {:.2f}s counts {:.0f} ADU -> {:.2f}s{}'
                              .format(exposure, counts, clamped_exposure, clamped_desc))

                        log.info('opsd', 'autoflat: {} {:.2f}s {:.0f} ADU -> {:.2f}s{}'
                                 .format(arm, exposure, counts, clamped_exposure, clamped_desc))

                        if is_evening:
                            # Sky is decreasing in brightness
                            # TODO: Remove this once we account for sun elevation?
                            for min_exposure in config['evening_exposure_delays']:
                                if new_exposure < min_exposure \
                                        and counts > config['min_save_counts']:
                                    delay = config['evening_exposure_delays'][min_exposure]
                                    print(arm + ' waiting ' + str(delay) + 's for it to get darker')
                                    time.sleep(delay)

                            if clamped_exposure == config['max_exposure'] \
                                    and counts < config['min_save_counts']:
                                state[arm] = AutoFlatState.Complete
                            elif state[arm] == AutoFlatState.Waiting \
                                    and counts > config['min_save_counts'] \
                                    and new_exposure > config['min_save_exposure']:
                                state[arm] = AutoFlatState.Saving
                        else:
                            # Sky is increasing in brightness
                            if clamped_exposure < config['min_save_exposure']:
                                state[arm] = AutoFlatState.Complete
                            elif state[arm] == AutoFlatState.Waiting \
                                    and counts > config['min_save_counts']:
                                state[arm] = AutoFlatState.Saving

                        if state[arm] != last_state:
                            with daemons.onemetre_pipeline.connect() as pipeline:
                                if state[arm] == AutoFlatState.Saving:
                                    pipeline.set_archive(arm, True)
                                else:
                                    pipeline.set_archive(arm, False)

                        if last_state != state[arm]:
                            print('autoflat: ' + arm + ' ' + AutoFlatState.Names[last_state] \
                                + ' -> ' + AutoFlatState.Names[state[arm]])
                            log.info('opsd', 'autoflat: {} arm {} -> {}'.format(
                                arm, AutoFlatState.Names[last_state],
                                AutoFlatState.Names[state[arm]]))

                        if state[arm] != AutoFlatState.Complete:
                            with camera_daemons[arm].connect() as cam:
                                cam.set_exposure(clamped_exposure)
                                cam.start_sequence(1)
                except Exception as e:
                    print('autoflat: failed to parse frame callback for arm ' + str(arm))
                    print(e)
                    if arm is not None:
                        state[arm] = AutoFlatState.Complete

            self._frame_watchers.append(received_frame)
            try:
                with self._telescope_condition:
                    self._telescope_active = True
                    # Give up early if the cameras are running
                    for arm in camera_daemons:
                        with camera_daemons[arm].connect() as cam:
                            if cam.report_status()['state'] != CameraStatus.Idle:
                                return CommandStatus.CameraActive

                    with daemons.onemetre_pipeline.connect() as pipeline:
                        pipeline.set_intensity_stats(True)
                        pipeline.set_output_frame_prefix('flat')
                        pipeline.set_frame_type('FLAT')
                        for arm in camera_daemons:
                            pipeline.set_archive(arm, False)

                    # TODO: Wait for sun to get to about the right elevation to save shutter cycles
                    # Acquire bias frame
                    for arm in camera_daemons:
                        with camera_daemons[arm].connect() as cam:
                            cam.set_shutter(False)
                            cam.set_exposure(0)
                            cam.start_sequence(1)

                    while True:
                        self._telescope_condition.wait(5)
                        if all([state[arm] == AutoFlatState.Complete for arm in state]):
                            return CommandStatus.Succeeded

                        if not self._telescope_active:
                            return CommandStatus.Failed

                return CommandStatus.Succeeded
            except Exception as e:
                print('autoflat: caught exception:')
                print(e)
                return CommandStatus.Failed
            finally:
                self._telescope_active = False
                self._frame_watchers.remove(received_frame)
                with daemons.onemetre_pipeline.connect() as pipeline:
                    pipeline.set_intensity_stats(False)

    # TODO: Rewrite using a dedicated telescope thread and action classes (see telcontrol branch)
    def acquire_field(self, ra_radians, dec_radians):
        """Placeholder logic to home the telescope in on a target position using WCS coordinates"""
        config = self._autoacquire_config
        with TryLock(self._telescope_lock) as success:
            if not success:
                return CommandStatus.Blocked

            # Coordinates extracted from the frame
            coordinates = {
                'BLUE': None,
                'RED': None,
            }

            processed = {
                'BLUE': False,
                'RED': False
            }

            camera_daemons = {
                'BLUE': daemons.onemetre_blue_camera,
                'RED': daemons.onemetre_red_camera
            }

            target = SkyCoord(ra_radians, dec_radians, unit='radian')

            def received_frame(headers):
                """Callback to process an acquired frame. headers is a dictionary of header keys"""
                arm = None
                try:
                    arm = headers['INSTRARM']

                    # Read WCS cooordinates from frame and convert to (ra,dec) in radians at center
                    if 'CTYPE1' in headers and 'CTYPE2' in headers:
                        w = WCS(headers)
                        # TODO: transform this to the center of the FOV (accounting for windowing)
                        center_x = headers['NAXIS1'] / 2
                        center_y = headers['NAXIS2'] / 2
                        ra_deg, dec_deg = w.all_pix2world(center_x, center_y, 0)

                        pos = SkyCoord(ra_deg, dec_deg, unit='deg')
                        pos_str = pos.to_string('hmsdms', sep=':')
                        print('autoacquire: solved {} field center: {}'.format(arm, pos_str))
                        coordinates[arm] = pos
                except Exception as e:
                    print('autoacquire: failed to parse frame callback for arm ' + str(arm))
                    print(e)
                finally:
                    processed[arm] = True

                    # Wake up the main thread if all frames have been received
                    if all([processed[arm] for arm in processed]):
                        with self._telescope_condition:
                            self._telescope_condition.notify_all()

            self._frame_watchers.append(received_frame)
            try:
                with self._telescope_condition:
                    self._telescope_active = True
                    # Give up early if the cameras are running
                    for arm in camera_daemons:
                        with camera_daemons[arm].connect() as cam:
                            if cam.report_status()['state'] != CameraStatus.Idle:
                                return CommandStatus.CameraActive

                    # Give up early if the drive power is disabled
                    with daemons.onemetre_power.connect() as power:
                        if not power.value('telescope_80v'):
                            return CommandStatus.TelescopeSlewFailed

                    with daemons.onemetre_pipeline.connect() as pipeline:
                        pipeline.set_wcs(True)
                        for arm in camera_daemons:
                            pipeline.set_archive(arm, False)

                    # Move the telescope to the requested coordinates
                    # This will be accurate to within a few arcmin
                    with daemons.onemetre_telescope.connect(timeout=config['slew_timeout']) as tel:
                        target_str = target.to_string('hmsdms', sep=':')
                        print('autoacquire: slewing to {}'.format(target_str))
                        log.info('opsd', 'autoacquire: slewing telescope to {}'.format(
                            target_str))
                        telstatus = tel.track_radec(ra_radians, dec_radians)
                        if telstatus != CommandStatus.Succeeded:
                            print('autoacquire: failed to move telescope with error ' \
                                  + str(telstatus))
                            log.info('opsd', 'autoacquire: slew failed')
                            return CommandStatus.TelescopeSlewFailed

                    # Wait a few of seconds for the telescope position to stabilize
                    time.sleep(5)

                    # Refine positioning by taking images and using the WCS to adjust
                    adjust_attempts = 0
                    wcs_attempts = 0
                    while True:
                        for arm in camera_daemons:
                            processed[arm] = False
                            coordinates[arm] = None
                            print('autoacquire: starting test image in ' + arm)
                            with camera_daemons[arm].connect() as cam:
                                # todo: reset windowing
                                cam.set_shutter(True)
                                cam.set_exposure(config['exposure_length'])
                                cam.start_sequence(1)

                        # Wait for exposure + max timeout
                        # Will be woken up early once all frames are received
                        wait = config['exposure_length'] + config['max_readout_process_time']
                        self._telescope_condition.wait(wait)
                        if not self._telescope_active:
                            return CommandStatus.Failed

                        blue_str = None
                        if coordinates['BLUE'] is not None:
                            blue_str = coordinates['BLUE'].to_string('hmsdms', sep=':')

                        red_str = None
                        if coordinates['RED'] is not None:
                            red_str = coordinates['RED'].to_string('hmsdms', sep=':')

                        print('autoacquire: solved field centers:')
                        print('   BLUE: ', blue_str)
                        print('   RED: ', red_str)

                        position = None
                        position_arm = None
                        if coordinates['BLUE'] is not None:
                            position = coordinates['BLUE']
                            position_arm = 'BLUE'
                        elif coordinates['RED'] is not None:
                            c = coordinates['RED']
                            o = config['red_camera_offset']
                            position = SkyCoord(c.ra + o[0], c.dec + o[1])
                            position_arm = 'RED'

                        if wcs_attempts >= config['wcs_attempts']:
                            log.info('opsd', 'autoacquire: WCS failed')
                            return CommandStatus.CoordinateSolutionFailed

                        if position is None:
                            wcs_attempts += 1
                            print('autoacquire: WCS attempt {} of {} failed'.format(
                                wcs_attempts, config['wcs_attempts']))

                            # return to start of loop to try another exposure
                            continue
                        else:
                            wcs_attempts = 0

                        if not self._telescope_active:
                            return CommandStatus.Failed

                        # TODO: Requires newer astropy!
                        # offset = position.spherical_offsets_to(target)
                        delta_ra = target.ra - position.ra
                        delta_dec = target.dec - position.dec
                        separation = position.separation(target).arcsecond

                        if adjust_attempts > config['adjust_attempts']:
                            print('autoacquire: giving up after ' + str(adjust_attempts) \
                                  + ' attempts with {:.2f} arcsec delta'.format(separation))
                            log.info('opsd', 'autoacquire: pointing failed to converge')
                            return CommandStatus.Failed

                        # Apply offset or break if close enough
                        if separation < config['pointing_threshold']:
                            print('autoacquire: reached target with delta {:.2f} arcsec'.format(
                                separation))
                            log.info('opsd', 'autoacquire: pointing complete')
                            return CommandStatus.Succeeded

                        # pylint: disable=no-member
                        delta_ra_str = delta_ra.to_string(u.hourangle, sep=':')
                        # pylint: enable=no-member
                        delta_dec_str = delta_dec.to_string(sep=':')
                        print('autoacquire: applying offset from {}: {}, {}'.format(
                            position_arm, delta_ra_str, delta_dec_str))
                        log.info('opsd', 'autoacquire: offsetting telescope by {} {}'
                                 .format(delta_ra_str, delta_dec_str))

                        with daemons.onemetre_telescope.connect(
                            timeout=config['slew_timeout']) as tel:

                            telstatus = tel.offset_radec(delta_ra.radian, delta_dec.radian)
                            if telstatus != CommandStatus.Succeeded:
                                print('autoacquire: failed to move telescope with error ' \
                                      + str(telstatus))
                                return CommandStatus.TelescopeSlewFailed

                        # Wait a few of seconds for the telescope position to stabilize
                        time.sleep(5)
                        adjust_attempts += 1
                        if not self._telescope_active:
                            return CommandStatus.Failed

            except Exception as e:
                print('autoacquire: caught exception:')
                print(e)
                return CommandStatus.Failed
            finally:
                self._telescope_active = False
                self._frame_watchers.remove(received_frame)

    # TODO: Rewrite using a dedicated telescope thread and action classes (see telcontrol branch)
    def stop(self):
        """Placeholder logic to cancel the active telescope task"""
        self._telescope_active = False
        with self._telescope_condition:
            self._telescope_condition.notify_all()

        return CommandStatus.Succeeded
