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

"""Script to display the results from a focus sweep action"""

import argparse
import glob
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
import os

from astropy.io import fits
import matplotlib
from matplotlib.backend_bases import MouseEvent
import matplotlib.pyplot as plt
import numpy as np
from rockit.common import daemons

class Cursor:
    def __init__(self, ax):
        self.ax = ax
        self.horizontal_line = ax.axhline(color='w', lw=0.8, ls='--')
        self.vertical_line = ax.axvline(color='w', lw=0.8, ls='--')
        self.text = ax.text(0.015, 0.98, '', va='top', transform=ax.transAxes)

    def set_cross_hair_visible(self, visible):
        need_redraw = self.horizontal_line.get_visible() != visible
        self.horizontal_line.set_visible(visible)
        self.vertical_line.set_visible(visible)
        self.text.set_visible(visible)
        return need_redraw

    def on_mouse_move(self, event):
        if not event.inaxes:
            need_redraw = self.set_cross_hair_visible(False)
            if need_redraw:
                self.ax.figure.canvas.draw()
        else:
            self.set_cross_hair_visible(True)
            self.horizontal_line.set_ydata([event.ydata])
            self.vertical_line.set_xdata([event.xdata])
            self.text.set_text(f'Cursor: {event.xdata:1.0f} steps')
            self.ax.figure.canvas.draw()

def plot_focus_sweep(prefix, args):
    """plot an automated focus sweep action"""
    parser = argparse.ArgumentParser(prefix)
    parser.add_argument('--subdir', type=str,
                        help='pipeline subdir where images were saved')
    parser.add_argument('prefix', type=str, nargs='+',
                        help='filename prefix(es) for the saved images')
    args = parser.parse_args(args)

    subdir = args.subdir
    if subdir is None:
        with daemons.warwick_pipeline.connect() as pipeline:
            subdir = pipeline.report_status()['archive_subdirectory']

    paths = []
    for prefix in args.prefix:
        paths.extend(glob.glob(f'/data/{subdir}/{prefix}-*.fits'))

    focuses = []
    hfds = []
    temperatures = []
    for path in paths:
        h = fits.getheader(path)
        if 'TELFOC' not in h or 'MEDHFD' not in h or 'DOMETEMP' not in h:
            continue

        focuses.append(h['TELFOC'])
        hfds.append(h['MEDHFD'])
        temperatures.append(h['DOMETEMP'])

    if len(focuses) == 0:
        print('error: no images found')
        return

    min_focus = np.floor(min(focuses) / 5000 - 0.5) * 5000
    max_focus = np.ceil(max(focuses) / 5000 + 0.5) * 5000
    min_hfd = np.floor(min(hfds) - 0.5)
    max_hfd = np.ceil(max(hfds) + 0.5)
    min_temperature = np.floor(min(temperatures) - 0.5)
    max_temperature = np.ceil(max(temperatures) + 0.5)

    # Suppress MESA-INTEL FINISHME warnings
    os.environ['GSK_RENDERER'] = 'opengl'

    settings = Gtk.Settings.get_default()
    settings.set_property("gtk-application-prefer-dark-theme", True)
    matplotlib.use('GTK4Agg')
    plt.style.use('dark_background')
    plt.rcParams['font.sans-serif'] = ['Red Hat Text']

    fig = plt.figure('Focus Plot')
    ax = plt.subplot(111)
    cmap = ax.scatter(focuses, hfds, c=temperatures, cmap='plasma', norm=plt.Normalize(min_temperature, max_temperature))
    ax.patch.set_facecolor('#1d1d20')
    fig.patch.set_facecolor('#1d1d20')
    ax.set_xlim(min_focus, max_focus)
    ax.set_ylim(min_hfd, max_hfd)
    ax.set_xlabel('Focus Position (steps)')
    ax.set_ylabel('Half flux diameter (arcsec)')

    cbar = plt.colorbar(cmap)
    cbar.set_label(r'Temperature ($^\circ$C)')

    cursor = Cursor(ax)
    fig.canvas.mpl_connect('motion_notify_event', cursor.on_mouse_move)
    MouseEvent('motion_notify_event', fig.canvas,
               *ax.transData.transform(((max_focus + min_focus) / 2, 0.5)))._process()

    fig.tight_layout()
    plt.show()
