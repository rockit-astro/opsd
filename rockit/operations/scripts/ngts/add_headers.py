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

"""add hfd and/or wcs headers to existing images"""

import argparse
import glob
from importlib.machinery import SourceFileLoader
import json
import os
import sys
from astropy.io import fits
import sep
from rockit.common import TFmt


def add_headers(prefix, args):
    """add hfd and/or wcs headers to existing images"""
    parser = argparse.ArgumentParser(prefix)
    parser.add_argument('--type', type=str, nargs='+', choices=['wcs', 'hfd'],
                        default=['wcs', 'hfd'], help='type of headers to add')
    parser.add_argument('file', type=str, nargs='+')
    args = parser.parse_args(args)

    if 'PIPELINED_CONFIG_PATH' in os.environ:
        config_path = os.environ['PIPELINED_CONFIG_PATH']
    else:
        paths = glob.glob("/etc/pipelined/*.json")
        if len(paths) != 1:
            print('error: failed to guess the default config file. ' +
                  f'Run as PIPELINED_CONFIG_PATH=/path/to/config.json {prefix}')
            return 1

        config_path = paths[0]

    pipeline_workerd = SourceFileLoader("pipelined", "/usr/bin/pipeline_workerd").load_module()
    with open(config_path, 'r', encoding='utf-8') as f:
        pipeline_config = json.load(f)

    max_filename_length = 0
    for path in args.file:
        max_filename_length = max(max_filename_length, len(os.path.basename(path)))

    try:
        # Disable terminal cursor
        sys.stdout.write('\033[?25l')

        for path in args.file:
            filename = os.path.basename(path)
            padding = ' ' * (1 + max_filename_length - len(os.path.basename(path)))

            if not os.access(path, os.R_OK | os.W_OK):
                print(f'Processing {filename}...{padding}{TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear} (not writable)')
                continue

            sys.stdout.write(f'Processing {filename}...{padding}')
            sys.stdout.flush()

            try:
                process_image(path, pipeline_workerd, pipeline_config, 'hfd' in args.type, 'wcs' in args.type)
                print(f'\r\033[KProcessing {filename}...{padding}{TFmt.Bold}{TFmt.Green}COMPLETE{TFmt.Clear}')
            except Exception:
                print(f'\r\033[KProcessing {filename}...{padding}{TFmt.Bold}{TFmt.Red}FAILED{TFmt.Clear}')
                sys.stdout.flush()

    finally:
        # Restore cursor
        sys.stdout.write('\033[?25h')


def process_image(path, pipeline_workerd, pipeline_config, calculate_hfd, calculate_wcs):
    data_raw, header = fits.getdata(path, header=True)
    camera_config = pipeline_config['cameras'][header['CAMID']]

    header_block_capacity = len(header) // 36 + 1
    while header.cards[-1][0] == '' and header.cards[-1][1] == '':
        header.pop()

    data_cropped, crop_x, crop_y = pipeline_workerd.window_header_region(
        header, data_raw, camera_config.get('image_region_card', None))

    data_bgsubtracted = data_cropped.astype(float)
    data_background = sep.Background(data_bgsubtracted)
    data_background.subfrom(data_bgsubtracted)

    binning = 1
    binning_card = camera_config.get('binning_card', None)
    if binning_card and binning_card in header:
        binning = header[binning_card]

    pipeline_workerd.move_bscale_bzero(header)

    objects = pipeline_workerd.detect_objects(
        data_bgsubtracted, data_background.globalrms, camera_config, binning, crop_x, crop_y)

    if calculate_hfd:
        pipeline_workerd.add_hfd_header(header, objects)

    if calculate_wcs:
        pipeline_workerd.add_wcs_header(header, objects, binning, False, camera_config.get('wcs', None))

    pipeline_workerd.write_fits_header(path, header, header_block_capacity)
